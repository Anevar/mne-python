# Authors: Christoph Dinh <chdinh@nmr.mgh.harvard.edu>
#          Martin Luessi <mluessi@nmr.mgh.harvard.edu>
#          Matti Hamalainen <msh@nmr.mgh.harvard.edu>
#          Alexandre Gramfort <gramfort@nmr.mgh.harvard.edu>
#          Denis Engemann <d.engemann@fz-juelich.de>
#
# License: BSD (3-clause)
import time
import copy

import numpy as np

from ..fiff import pick_channels, pick_types
from ..utils import logger, verbose
from ..baseline import rescale
from ..epochs import _BaseEpochs
from ..event import _find_events
from ..filter import detrend
from ..fiff.proj import setup_proj


class RtEpochs(_BaseEpochs):
    """Realtime Epochs

    Can receive epochs in real time from an RtClient.

    For example, to get some epochs from a running mne_rt_server on
    'localhost', you could use:

    client = mne.realtime.RtClient('localhost')
    event_id, tmin, tmax = 1, -0.2, 0.5

    epochs = mne.realtime.RtEpochs(client, event_id, tmin, tmax)
    epochs.start()  # start the measurement and start receiving epochs

    evoked_1 = epochs.average()  # computed over all epochs
    evoked_2 = epochs[-5:].average()  # computed over the last 5 epochs

    Parameters
    ----------
    client : instance of mne.realtime.RtClient
        The realtime client.
    event_id : int | list of int
        The id of the event to consider. If int, only events with the
        ID specified by event_id are considered. Multiple event ID's
        can be specified using a list.
    tmin : float
        Start time before event.
    tmax : float
        End time after event.
    stim_channel : string or list of string
        Name of the stim channel or all the stim channels affected by
        the trigger.
    sleep_time : float
        Time in seconds to wait between checking for new epochs when epochs
        are requested and the receive queue is empty.
    name : string
        Comment that describes the Evoked data created.
    baseline : None (default) or tuple of length 2
        The time interval to apply baseline correction.
        If None do not apply it. If baseline is (a, b)
        the interval is between "a (s)" and "b (s)".
        If a is None the beginning of the data is used
        and if b is None then b is set to the end of the interval.
        If baseline is equal to (None, None) all the time
        interval is used.
    reject : dict
        Epoch rejection parameters based on peak to peak amplitude.
        Valid keys are 'grad' | 'mag' | 'eeg' | 'eog' | 'ecg'.
        If reject is None then no rejection is done.
        Values are float. Example:
        reject = dict(grad=4000e-13, # T / m (gradiometers)
                      mag=4e-12, # T (magnetometers)
                      eeg=40e-6, # uV (EEG channels)
                      eog=250e-6 # uV (EOG channels)
                      )
    flat : dict
        Epoch rejection parameters based on flatness of signal
        Valid keys are 'grad' | 'mag' | 'eeg' | 'eog' | 'ecg'
        If flat is None then no rejection is done.
    proj : bool, optional
        Apply SSP projection vectors
    decim : int
        Factor by which to downsample the data from the raw file upon import.
        Warning: This simply selects every nth sample, data is not filtered
        here. If data is not properly filtered, aliasing artifacts may occur.
    reject_tmin : scalar | None
        Start of the time window used to reject epochs (with the default None,
        the window will start with tmin).
    reject_tmax : scalar | None
        End of the time window used to reject epochs (with the default None,
        the window will end with tmax).
    detrend : int | None
        If 0 or 1, the data channels (MEG and EEG) will be detrended when
        loaded. 0 is a constant (DC) detrend, 1 is a linear detrend. None
        is no detrending. Note that detrending is performed before baseline
        correction. If no DC offset is preferred (zeroth order detrending),
        either turn off baseline correction, as this may introduce a DC
        shift, or set baseline correction to use the entire time interval
        (will yield equivalent results but be slower).
    add_eeg_ref : bool
        If True, an EEG average reference will be added (unless one
        already exists).
    isi_max : float
        The maximmum time in seconds between epochs. If no epoch
        arrives in the next isi_max seconds the RtEpochs stops.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).
        Defaults to client.verbose.

    Attributes
    ----------
    info : dict
        Measurement info.
    event_id : dict
        Names of  of conditions corresponding to event_ids.
    ch_names : list of string
        List of channels' names.
    events : list of tuples
        The events associated with the epochs currently in the queue.
    verbose : bool, str, int, or None
        See above.
    """
    @verbose
    def __init__(self, client, event_id, tmin, tmax, stim_channel='STI 014',
                 sleep_time=0.1, baseline=(None, 0), picks=None,
                 name='Unknown', reject=None, flat=None, proj=True,
                 decim=1, reject_tmin=None, reject_tmax=None, detrend=None,
                 add_eeg_ref=True, isi_max=2., verbose=None):

        info = client.get_measurement_info()

        # the measurement info of the data as we receive it
        self._client_info = copy.deepcopy(info)

        verbose = client.verbose if verbose is None else verbose

        # call _BaseEpochs constructor
        super(RtEpochs, self).__init__(info, event_id, tmin, tmax,
                baseline=baseline, picks=picks, name=name, reject=reject,
                flat=flat, decim=decim, reject_tmin=reject_tmin,
                reject_tmax=reject_tmax, detrend=detrend,
                add_eeg_ref=add_eeg_ref, verbose=verbose)

        self.proj = proj
        self._projector, self.info = setup_proj(self.info, add_eeg_ref,
                                                activate=self.proj)

        self._client = client

        if not isinstance(stim_channel, list):
            stim_channel = [stim_channel]

        stim_picks = pick_channels(self._client_info['ch_names'],
                                   include=stim_channel, exclude=[])

        if len(stim_picks) == 0:
            raise ValueError('No stim channel found to extract event '
                             'triggers.')

        self._stim_picks = stim_picks

        self._sleep_time = sleep_time

        # add calibration factors
        cals = np.zeros(self._client_info['nchan'])
        for k in range(self._client_info['nchan']):
            cals[k] = (self._client_info['chs'][k]['range']
                       * self._client_info['chs'][k]['cal'])
        self._cals = cals[:, None]

        # FIFO queues for received epochs and events
        self._epoch_queue = list()
        self.events = list()

        # variables needed for receiving raw buffers
        self._last_buffer = None
        self._first_samp = 0
        self._event_backlog = list()

        # Number of good and bad epochs received
        self._n_good = 0
        self._n_bad = 0

        self._started = False
        self._last_time = time.time()

        self.isi_max = isi_max

    def start(self):
        """Start receiving epochs

        The measurement will be started if it has not already been started.
        """
        if not self._started:
            # register the callback
            self._client.register_receive_callback(self._process_raw_buffer)

            # start the measurement and the receive thread
            nchan = self._client_info['nchan']
            self._client.start_receive_thread(nchan)
            self._started = True

            self._last_time = np.inf  # init delay counter. Will stop iterations

    def stop(self, stop_receive_thread=False, stop_measurement=False):
        """Stop receiving epochs

        Parameters
        ----------
        stop_receive_thread : bool
            Stop the receive thread. Note: Other RtEpochs instances will also
            stop receiving epochs when the receive thread is stopped. The
            receive thread will always be stopped if stop_measurement is True.

        stop_measurement : bool
            Also stop the measurement. Note: Other clients attached to the
            server will also stop receiving data.
        """
        if self._started:
            self._client.unregister_receive_callback(self._process_raw_buffer)
            self._started = False

        if stop_receive_thread or stop_measurement:
            self._client.stop_receive_thread(stop_measurement=stop_measurement)

    def next(self, return_event_id=False):
        """To make iteration over epochs easy.
        """
        first = True
        while True:
            current_time = time.time()
            if current_time > (self._last_time + self.isi_max):
                logger.info('Time of %s seconds exceeded.' % self.isi_max)
                raise StopIteration
            if len(self._epoch_queue) > self._current:
                epoch = self._epoch_queue[self._current]
                event_id = self.events[self._current][-1]
                self._current += 1
                self._last_time = current_time
                if return_event_id:
                    return epoch, event_id
                else:
                    return epoch
            if self._started:
                if first:
                    logger.info('Waiting for epoch %d' % (self._current + 1))
                    first = False
                time.sleep(self._sleep_time)
            else:
                raise RuntimeError('Not enough epochs in queue and currently '
                                   'not receiving epochs, cannot get epochs!')

    def _get_data_from_disk(self):
        """Return the data for n_epochs epochs"""

        epochs = list()
        for epoch in self:
            epochs.append(epoch)

        data = np.array(epochs)

        return data

    def _process_raw_buffer(self, raw_buffer):
        """Process raw buffer (callback from RtClient)

        Note: Do not print log messages during regular use. It will be printed
        asynchronously which is annyoing when working in an interactive shell.

        Parameters
        ----------
        raw_buffer : array of float, shape=(nchan, n_times)
            The raw buffer.
        """
        verbose = 'ERROR'
        sfreq = self.info['sfreq']
        n_samp = len(self._raw_times)

        # relative start and stop positions in samples
        tmin_samp = int(round(sfreq * self.tmin))
        tmax_samp = tmin_samp + n_samp

        last_samp = self._first_samp + raw_buffer.shape[1] - 1

        # apply calibration without inplace modification
        raw_buffer = self._cals * raw_buffer

        # detect events
        data = np.abs(raw_buffer[self._stim_picks]).astype(np.int)
        data = np.atleast_2d(data)
        buff_events = _find_events(data, self._first_samp, verbose=verbose)

        events = self._event_backlog
        for event_id in self.event_id.values():
            idx = np.where(buff_events[:, -1] == event_id)[0]
            events.extend(zip(list(buff_events[idx, 0]),
                              list(buff_events[idx, -1])))

        events.sort()

        event_backlog = list()
        for event_samp, event_id in events:
            epoch = None
            if (event_samp + tmin_samp >= self._first_samp
                    and event_samp + tmax_samp <= last_samp):
                # easy case: whole epoch is in this buffer
                start = event_samp + tmin_samp - self._first_samp
                stop = event_samp + tmax_samp - self._first_samp
                epoch = raw_buffer[:, start:stop]
            elif (event_samp + tmin_samp < self._first_samp
                    and event_samp + tmax_samp <= last_samp):
                # have to use some samples from previous buffer
                if self._last_buffer is None:
                    continue
                n_last = self._first_samp - (event_samp + tmin_samp)
                n_this = n_samp - n_last
                epoch = np.c_[self._last_buffer[:, -n_last:],
                              raw_buffer[:, :n_this]]
            elif event_samp + tmax_samp > last_samp:
                # we need samples from next buffer
                if event_samp + tmin_samp < self._first_samp:
                    raise RuntimeError('Epoch spans more than two raw '
                                       'buffers, increase buffer size!')
                # we will process this epoch with the next buffer
                event_backlog.append((event_samp, event_id))
            else:
                raise RuntimeError('Unhandled case..')

            if epoch is not None:
                self._append_epoch_to_queue(epoch, event_samp, event_id)

        # set things up for processing of next buffer
        self._event_backlog = event_backlog
        self._first_samp = last_samp + 1
        self._last_buffer = raw_buffer

    def _append_epoch_to_queue(self, epoch, event_samp, event_id):
        """Append a (raw) epoch to queue

        Note: Do not print log messages during regular use. It will be printed
        asynchronously which is annyoing when working in an interactive shell.

        Parameters
        ----------
        epoch : array of float, shape=(nchan, n_times)
            The raw epoch (only calibration has been applied) over all
            channels.
        event_samp : int
            The time in samples when the epoch occurred.
        event_id : int
            The event ID of the epoch.
        """
        # select the channels
        epoch = epoch[self.picks, :]

        # handle offset
        if self._offset is not None:
            epoch += self._offset

        # apply SSP
        if self.proj and self._projector is not None:
            epoch = np.dot(self._projector, epoch)

        # Detrend
        if self.detrend is not None:
            picks = pick_types(self.info, meg=True, eeg=True, stim=False,
                               eog=False, ecg=False, emg=False, ref_meg=False)
            epoch[picks] = detrend(epoch[picks], self.detrend, axis=1)

        # Baseline correct
        epoch = rescale(epoch, self._raw_times, self.baseline, 'mean',
                        copy=False, verbose='ERROR')

        # Decimate
        if self.decim > 1:
            epoch = epoch[:, self._decim_idx]

        # Decide if this is a good epoch
        is_good, _ = self._is_good_epoch(epoch, verbose='ERROR')

        if is_good:
            self._epoch_queue.append(epoch)
            self.events.append((event_samp, 0, event_id))
            self._n_good += 1
        else:
            self._n_bad += 1

    def __repr__(self):
        s = 'good / bad epochs received: %d / %d, epochs in queue: %d, '\
            % (self._n_good, self._n_bad, len(self._epoch_queue))
        s += ', tmin : %s (s)' % self.tmin
        s += ', tmax : %s (s)' % self.tmax
        s += ', baseline : %s' % str(self.baseline)
        return '<RtEpochs  |  %s>' % s
