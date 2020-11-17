from ctapipe.core import TelescopeComponent
from ctapipe.core.traits import TelescopeParameter, Enum, Int, Bool
from astropy.time import Time
import numpy as np
from collections import deque, defaultdict


CENTRAL_MODULE = 132


# fix for https://github.com/ipython/traitlets/issues/637
class Int(Int):
    def validate(self, obj, value):
        if value is None and self.allow_none is True:
            return value

        return super().validate(obj, value)


def calc_dragon_time(lst_event_container, central_module_index, reference):
    return (
        reference
        + lst_event_container.evt.pps_counter[central_module_index]
        + lst_event_container.evt.tenMHz_counter[central_module_index] * 1e-7
    )


def calc_tib_time(lst_event_container, reference):
    return (
        reference
        + lst_event_container.evt.tib_pps_counter
        + lst_event_container.evt.tib_tenMHz_counter * 1e-7
    )


class EventTimeCalculator(TelescopeComponent):
    '''
    From low-level counter / time information compute the correct event timestamp
    '''

    timestamp = TelescopeParameter(
        trait=Enum(['ucts', 'dragon', 'tib']), default_value='dragon'
    ).tag(config=True)

    ucts_t0_dragon = TelescopeParameter(
        Int(allow_none=True),
        default_value=None,
        help='UCTS timestamp of a valid ucts/dragon counter combination'
    ).tag(config=True)

    dragon_counter0 = TelescopeParameter(
        Int(allow_none=True),
        help='Dragon board counter value of a valid ucts/dragon counter combination',
        default_value=None,
    ).tag(config=True)

    ucts_t0_tib = TelescopeParameter(
        Int(allow_none=True),
        default_value=None,
        help='UCTS timestamp of a valid ucts/tib counter combination'
    ).tag(config=True)
    tib_counter0 = TelescopeParameter(
        Int(allow_none=True),
        default_value=None,
        help='TIB board counter value of a valid ucts/tib counter combination'
    ).tag(config=True)

    use_first_event = Bool(default_value=True).tag(config=True)

    def __init__(self, subarray, config=None, parent=None, **kwargs):
        super().__init__(subarray=subarray, config=config, parent=parent, **kwargs)

        self.first_valid_ucts = dict()
        self.first_valid_ucts_tib = dict()
        self.previous_ucts_times = defaultdict(deque)
        self.previous_ucts_trigger_types = defaultdict(deque)

        self._has_reference = {}

        # we cannot __setitem__ telescope lookup values, so we store them
        # in non-trait private values
        self._ucts_t0_dragon = {}
        self._dragon_counter0 = {}
        self._ucts_t0_tib = {}
        self._tib_counter0 = {}

        for tel_id in self.subarray.tel:
            self._has_reference[tel_id] = all([
                self.ucts_t0_dragon.tel[tel_id],
                self.dragon_counter0.tel[tel_id],
                self.ucts_t0_tib.tel[tel_id],
                self.tib_counter0.tel[tel_id],
            ])

            if self._has_reference[tel_id]:
                self._ucts_t0_dragon[tel_id] = self.ucts_t0_dragon.tel[tel_id]
                self._dragon_counter0[tel_id] = self.dragon_counter0.tel[tel_id]
                self._ucts_t0_tib[tel_id] = self.ucts_t0_tib.tel[tel_id]
                self._tib_counter0[tel_id] = self.tib_counter0.tel[tel_id]
            else:
                if not self.use_first_event:
                    raise ValueError(
                        'No external reference timestamps/counter values provided'
                        ' and ``use_first_event`` is False'
                    )
                else:
                    self.log.warning(
                        'Using first event as time reference for counters,'
                        ' this will lead to wrong timestamps / trigger types'
                        ' for all but the first subrun'
                    )

    def __call__(self, tel_id, event):
        lst = event.lst.tel[tel_id]

        # data comes in random module order, svc contains actual order
        central_module_index = np.where(lst.svc.module_ids == CENTRAL_MODULE)[0][0]

        if self._has_reference[tel_id]:
            # Dragon/TIB timestamps based on a valid absolute reference UCTS timestamp
            dragon_time = calc_dragon_time(
                lst, central_module_index,
                reference=1e-9 * (self._ucts_t0_dragon[tel_id] - self._dragon_counter0[tel_id])
            )

            tib_time = calc_tib_time(
                lst,
                reference=1e-9 * (self._ucts_t0_tib[tel_id] - self._tib_counter0[tel_id])
            )

            if lst.evt.extdevices_presence & 2:
                # UCTS presence flag is OK
                ucts_timestamp = lst.evt.ucts_timestamp
                ucts_time = ucts_timestamp * 1e-9  # secs

                if tel_id not in self.first_valid_ucts:
                    self.first_valid_ucts[tel_id] = ucts_time

                if tel_id not in self.first_valid_ucts_tib and lst.evt.extdevices_presence & 1:
                    self.first_valid_ucts_tib[tel_id] = ucts_time
            else:
                ucts_time = np.nan

        # first event and values not passed
        else:
            if not lst.evt.extdevices_presence & 2:
                raise ValueError(
                    'Timestamp reference should be extracted from first event'
                    ' but UCTS not available'
                )

            ucts_timestamp = lst.evt.ucts_timestamp
            ucts_time = ucts_timestamp * 1e-9  # secs

            initial_dragon_counter = (
                int(1e9) * lst.evt.pps_counter[central_module_index]
                + 100 * lst.evt.tenMHz_counter[central_module_index]
            )

            self._ucts_t0_dragon[tel_id] = ucts_timestamp
            self._dragon_counter0[tel_id] = initial_dragon_counter
            self.log.info(
                'Using first event as time reference for dragon.'
                f' UCTS timestamp: {ucts_timestamp}'
                f' dragon_counter: {initial_dragon_counter}'
            )

            if not lst.evt.extdevices_presence & 1 and self.timestamp == 'tib':
                raise ValueError(
                    'TIB is selected for timestamp, no external reference given'
                    ' and first event has not TIB info'
                )

            initial_tib_counter = (
                int(1e9) * lst.evt.tib_pps_counter
                + 100 * lst.evt.tib_tenMHz_counter
            )
            self._ucts_t0_tib[tel_id] = ucts_timestamp
            self._tib_counter0[tel_id] = initial_tib_counter
            self.log.info(
                'Using first event as time reference for TIB.'
                f' UCTS timestamp: {ucts_timestamp}'
                f' tib_counter: {initial_tib_counter}'
            )

            tib_time = ucts_time
            dragon_time = ucts_time
            self._has_reference[tel_id] = True

        # Due to a DAQ bug, sometimes there are 'jumps' in the
        # UCTS info in the raw files. After one such jump,
        # all the UCTS info attached to an event actually
        # corresponds to the next event. This one-event
        # shift stays like that until there is another jump
        # (then it becomes a 2-event shift and so on). We will
        # keep track of those jumps, by storing the UCTS info
        # of the previously read events in the list
        # previous_ucts_time_unix. The list has one element
        # for each of the jumps, so if there has been just
        # one jump we have the UCTS info of the previous
        # event only (which truly corresponds to the
        # current event). If there have been n jumps, we keep
        # the past n events. The info to be used for
        # the current event is always the first element of
        # the array, previous_ucts_time_unix[0], whereas the
        # current event's (wrong) ucts info is placed last in
        # the array. Each time the first array element is
        # used, it is removed and the rest move up in the
        # list. We have another similar array for the trigger
        # types, previous_ucts_trigger_type
        ucts_trigger_type = lst.evt.ucts_trigger_type

        if len(self.previous_ucts_times[tel_id]) > 0:
            # keep the time & trigger type read for this
            # event (which really correspond to a later event):
            current_ucts_time = ucts_time
            current_ucts_trigger_type = ucts_trigger_type

            # get the correct time for the current event from the queue
            ucts_time = self.previous_ucts_times[tel_id].popleft()
            ucts_trigger_type = self.previous_ucts_trigger_types[tel_id].popleft()

            # now put the current values last in the queue, for later use:
            self.previous_ucts_times[tel_id].append(current_ucts_time)
            self.previous_ucts_trigger_types[tel_id].append(current_ucts_trigger_type)

            lst.evt.ucts_trigger_type = ucts_trigger_type
            lst.evt.ucts_timestamp = ucts_time * 1e9

        # Now check consistency of UCTS and Dragon times. If
        # UCTS time is ahead of Dragon time by more than
        # 1.e-6 s, most likely the UCTS info has been
        # lost for this event (i.e. there has been another
        # 'jump' of those described above), and the one we have
        # actually corresponds to the next event. So we put it
        # back first in the list, to assign it to the next
        # event. We also move the other elements down in the
        # list,  which will now be one element longer.
        # We leave the current event with the same time,
        # which will be approximately correct (depending on
        # event rate), and set its ucts_trigger_type to -1,
        # which will tell us a jump happened and hence this
        # event does not have proper UCTS info.
        if (ucts_time - dragon_time) > 1e-6:
            self.log.warning(
                f'Found UCTS jump, dragon time: {dragon_time:.07f}'
                f', delta: {ucts_time - dragon_time:.7f} s'
            )
            self.previous_ucts_times[tel_id].appendleft(ucts_time)
            self.previous_ucts_trigger_types[tel_id].appendleft(ucts_trigger_type)
            ucts_trigger_type = -1

        # Select the timestamps to be used for pointing interpolation
        if self.timestamp.tel[tel_id] == "ucts":
            timestamp = Time(ucts_time, format='unix_tai')

        elif self.timestamp.tel[tel_id] == "dragon":
            timestamp = Time(dragon_time, format='unix_tai')

        elif self.timestamp.tel[tel_id] == "tib":
            timestamp = Time(tib_time, format='unix_tai')

        self.log.debug(f'tib: {tib_time:.7f}, dragon: {dragon_time:.7f}, ucts: {ucts_time:.7f}')

        return timestamp