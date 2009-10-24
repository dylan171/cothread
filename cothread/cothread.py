# This file is part of the Diamond cothread library.
#
# Copyright (C) 2007 James Rowland, 2007-2008 Michael Abbott,
# Diamond Light Source Ltd.
#
# The Diamond cothread library is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the License,
# or (at your option) any later version.
#
# The Diamond cothread library is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin St, Fifth Floor, Boston, MA 02110-1301 USA
#
# Contact:
#      Dr. Michael Abbott,
#      Diamond Light Source Ltd,
#      Diamond House,
#      Chilton,
#      Didcot,
#      Oxfordshire,
#      OX11 0DE
#      michael.abbott@diamond.ac.uk

'''Simple cooperative threading using coroutines.  The following functions
define the interface provided by this module.

    Spawn(function, arguments...)
        A new cooperative thread, or "task", is created as a call to 
        function(arguments).  Control is not transferred to the task until
        control is yielded.

    Sleep(delay)
    SleepUntil(time)
        The calling task is suspended until the given time.  Sleep(delay)
        suspends the task for at least delay seconds, SleepUntil(time)
        suspends until the specified time has passed (time is defined as the
        value returned by time.time()).
            Control is not returned to the calling task until all other
        active tasks have been processed.
        
    Yield()
        Yield() suspends control so that all other potentially busy tasks can
        run.  

Instances of the Event object can be used for communication between tasks.
The following Event object methods are relevant.

    Wait()
    Wait(timeout)
        Waits for the event object to be signalled or for the timeout to
        expire (if specified).  Returns True if a signal was received, False
        if a timeout ocurred.

    Signal()
        Signals the event object, releasing at least one waiting task.

Similarly the EventQueue can be used for communication.
'''

# It might be worth taking a close look at:
#   http://wiki.secondlife.com/wiki/Eventlet

import sys
import os
import time
import py.magic as greenlet
greenlet.getcurrent = greenlet.greenlet.getcurrent
import bisect
import traceback
import collections 
import thread

import coselect


__all__ = [
    'Spawn',            # Spawn new task
    
    'Sleep',            # Suspend task for given delay
    'SleepUntil',       # Suspend task until specified time
    'Yield',            # Suspend task for immediate resumption
    
    'Event',            # Event for waiting and signalling
    'EventQueue',       # Queue of objects with event handling
    'ThreadedEventQueue',   # Event queue designed to work with threads
    'WaitForAll',       # Wait for all events to become ready

    'AbsTimeout',       # Converts timeout into absolute deadline format
    'Timedout',         # Timeout exception raised by event waiting
    
    'Quit',             # Immediate process quit
    'WaitForQuit',      # Wait until Quit() is called

    'Timer',            # One-shot cancellable timer
]




class _TimerQueue(object):
    '''A timer queue: objects are held on the queue in timeout sequence.'''

    # The queue is implemented using the bisect function to insert objects
    # into the queue without having to resort the list.  This is cheap and
    # cheerful to implement and runs fast enough.
    
    def __init__(self):
        # The queue is a list of (timeout, task) pairs -- it's important that
        # the timeout is first so that bisect searching of the queue works
        # properly.
        self.__queue = []
        self.__garbage = 0
        
    def put(self, task, timeout):
        '''Adds value to the queue with the specified timeout.'''
        index = bisect.bisect(self.__queue, (timeout, None))
        self.__queue.insert(index, (timeout, task))

    def timeout(self):
        '''Returns the timeout of the queue.  Only valid if queue not empty.'''
        return self.__queue[0][0]

    def wake_expired(self):
        index = bisect.bisect_right(self.__queue, (time.time(), None))
        expired = self.__queue[:index]
        del self.__queue[:index]

        for _, task in expired:
            if not task.wakeup(_WAKEUP_TIMEOUT):
                self.__garbage -= 1
        assert 0 <= self.__garbage <= len(self)

    def __len__(self):
        '''Returns the number of entries on the queue.'''
        return len(self.__queue)

    def cancel(self):
        '''This is called to cancel a timeout.  We add this to our garbage
        count, triggering a garbage collect if appropriate.'''
        self.__garbage += 1
        if 2 * self.__garbage > len(self):
            self.__queue = [entry
                for entry in self.__queue
                if not entry[1].woken()]
            self.__garbage = 0


class _WakeupQueue(object):
    def __init__(self):
        self.__waiters = []
        # Every time a timeout occurs a waiter is left behind on the timer
        # queue.  We keep count of these as "garbage", and at the appropriate
        # time we can garbage collect the queue.
        self.__garbage = 0

    def __len__(self):
        return len(self.__waiters)

    def append(self, waiter):
        self.__waiters.append(waiter)

    def wake(self, wake_all):
        if self.__waiters:
            if wake_all:
                for task in self.__waiters:
                    task.wakeup(_WAKEUP_NORMAL)
                self.__waiters = []
                self.__garbage = 0
            else:
                # Wake the first task that actually wakes, mark the rest as
                # junk.
                for n, task in enumerate(self.__waiters):
                    if task.wakeup(_WAKEUP_NORMAL):
                        break
                    else:
                        self.__garbage -= 1
                del self.__waiters[:n+1]
        assert 0 <= self.__garbage <= len(self)

    def cancel(self):
        # A cancelled wait becomes garbage on the waiting queue.  We keep
        # count of how much garbage there is -- once the queue has more
        # garbage than waiters it's probably time to rebuild the queue and
        # keep only those waiters which haven't been woken yet.
        self.__garbage += 1
        if 2 * self.__garbage > len(self):
            self.__waiters = [task
                for task in self.__waiters
                if not task.woken()]
            self.__garbage = 0
        

class _Wakeup(object):
    '''A _Wakeup object is used when a task is to be suspended on one or more
    queues.  On wakeup the original task is woken, but only once: this is
    used to ensure that entries on other queues are effectively cancelled.'''
    def __init__(self, wakeup_task, queue, timers):
        self.__task = greenlet.getcurrent()
        self.__wakeup_task = wakeup_task
        self.__queue = queue
        self.__timers = timers
        
    def wakeup(self, reason):
        if self.__task:
            # Let the scheduler know that this task has been woken, and forget
            # about it, so we don't wake it again.
            #    Note that it's rather important to mark this wakeup as woken
            # *before* calling the queue cancel() functions, as otherwise
            # their garbage collection will be confused!
            self.__wakeup_task(self.__task, reason)
            self.__task = None
            
            # Each queue needs to be cancelled if it's not the wakeup reason.
            # This test also properly deals with _WAKEUP_INTERRUPT, which
            # requires both queues to be cancelled.
            if reason != _WAKEUP_NORMAL and self.__queue:
                self.__queue.cancel()
            if reason != _WAKEUP_TIMEOUT and self.__timers:
                self.__timers.cancel()

            # Also drop our reference to the queue to avoid overextending
            # object lifetime.
            self.__queue = None
            return True
        else:
            return False
        
    def woken(self):
        return self.__task is None


# Task wakeup reasons
_WAKEUP_NORMAL = 0     # Normal wakeup
_WAKEUP_TIMEOUT = 1    # Wakeup on timeout
_WAKEUP_INTERRUPT = 2  # Special: transfer scheduler exception to main


# Important system invariants:
#   - A running task is not on any waiting queue.
#       This is enforced by:
#       1) when a task it suspended it is recorded on waiting queues by using
#          a shared _Wakeup() object;
#       2) the .wakeup() method is always used before resuming the task.

class _Scheduler(object):
    '''Coroutine activity scheduler.'''

    @classmethod
    def create(cls):
        '''Creates the scheduler in its own coroutine and starts it running.
        We switch to the scheduler long enough for it to complete
        initialisation.'''
        # We run the scheduler in its own greenlet to allow the main task to
        # participate in scheduling.  This produces its own complications but
        # makes for a more usable system.
        scheduler_task = greenlet.greenlet(cls.__scheduler)
        return scheduler_task.switch(greenlet.getcurrent())

    @classmethod
    def __scheduler(cls, main_task):
        '''The top level scheduler loop.  Starts by creating the scheduler,
        and then manages dispatching from the top level.'''

        # First create the scheduler and pass it back to our caller, who we
        # expect to be the main task.  The next time we get control it's time
        # to run the scheduling loop.
        self = cls()
        main_task.switch(self)

        # If the schedule loop raises an exception then propagate the
        # exception up to the main thread before restarting the scheduler.
        # This has mostly the right effects: a standalone program will
        # terminate, and an interactive program will receive back control, and
        # the scheduler should carry on operating.
        while True:
            try:
                self.__schedule_loop()
            except:
                # Switch to the main task asking it to re-raise the interrupt.
                # First we have to make sure it's not on the run queue.
                for index, (task, reason) in enumerate(self.__ready_queue):
                    if task is main_task:
                        del self.__ready_queue[index]
                        break
                # All task wakeup entry points will interpret this as a 
                # request to re-raise the exception.
                main_task.switch(_WAKEUP_INTERRUPT)
        
    def __init__(self):
        # List of all tasks that are currently ready to be dispatched.
        self.__ready_queue = []
        # List of tasks waiting for ready_queue to become empty
        self.__yield_queue = _WakeupQueue()
        # List of tasks waiting for a timeout
        self.__timer_queue = _TimerQueue()
        # Scheduler greenlet: this will be switched to whenever any other
        # task decides to sleep.
        self.__greenlet = greenlet.getcurrent()
        # Initially the schedule loop will run freely with its own select.
        self.__poll_callback = None
        # Dictionary of waitable descriptors for which polling needs to be
        # done.  Each entry consists of an event mask together with a list of
        # interested tasks.
        self.__poll_queue = {}
        # By default use blocking poll while waiting for the next event.
        self._poll_block = coselect.poll_block
        

    def __tick(self):
        '''This must be called regularly to ensure that all waiting tasks are
        processed.  It processes all tasks that are ready to run and then runs
        all timers that have expired.'''
        # Wake up all the expired timers on entry.  These go to the end of
        # the ready queue.
        self.__timer_queue.wake_expired()
        # If the ready queue is still empty, now's the time to run the yield
        # queue.
        if not self.__ready_queue:
            self.__yield_queue.wake(True)
        
        # Pick up the ready queue and process every task in it.  When each
        # task is resumed it is passed a flag indicating whether it has been
        # resumed because of an expired timer, or for some other reason
        # (typically either a voluntary suspend, or a successful wait for an
        # event).
        ready_queue = self.__ready_queue
        self.__ready_queue = []
        for task, reason in ready_queue:
            assert not task.dead
            task.switch(reason)
            
    def __schedule_loop(self):
        '''This runs a scheduler loop without returning.'''
        while True:
            # Dispatch all waiting tasks
            self.__tick()
            
            # Now see how long we have to wait for the next tick
            if self.__ready_queue or self.__yield_queue:
                # There are ready tasks: don't wait
                delay = 0
            elif self.__timer_queue:
                # There are timers waiting to fire: wait for the first one.
                delay = max(self.__timer_queue.timeout() - time.time(), 0)
            else:
                # Nothing to do: block until something external happens.
                delay = None

            # Finally suspend until something is ready.
            self.__wakeup_poll(self.__poll_suspend(delay))

    def __poll_suspend(self, delay):
        '''Suspends the scheduler until the appropriate ready condition is
        reached.  Returns lists of ready file descriptors and events.'''
        poll_list, self.__poll_queue = \
            coselect._compute_poll_list(self.__poll_queue)
        if self.__poll_callback is None:
            # If we're not being polled from outside, run our own poll.
            return self._poll_block(poll_list, delay)
        else:
            # If the scheduler loop was invoked from outside then return
            # control back to the caller: it will provide the select
            # operation we need.
            return self.__poll_callback.switch(poll_list, delay)

    def poll_scheduler(self, ready_list):
        '''This is called when the scheduler needs to be controlled from
        outside.  It will perform a full round of scheduling before returing
        control to the caller.
            Two values are returned, a list of descriptors and events plus
        a timeout, being precisely the values required for a call to
        poll_block().  A sensible default outer scheduler loop would be

            ready_list = []
            while True:
                ready_list = poll_block(*poll_scheduler(ready_list))
        '''
        assert self.__poll_callback is None, 'Nested pollers will not work'
        
        # Switching to the scheduler will return control to us when the next
        # round is complete.
        #    Note that the first time this is called we may get an incomplete
        # schedule, as we may be resuming inside the dispatch loop: in effect
        # the first call to this routine interrupts the original scheduler.
        self.__poll_callback = greenlet.getcurrent()
        result = self.__greenlet.switch(ready_list)
        self.__poll_callback = None
        
        if result == _WAKEUP_INTERRUPT:
            # This case arises if we are main and the scheduler just died.
            raise
        else:
            return result
        

    def spawn(self, function):
        '''Spawns a new task: function is spawned as a new background task
        as a child of the scheduler task.'''
        task = greenlet.greenlet(function, self.__greenlet)
        self.__ready_queue.append((task, _WAKEUP_NORMAL))

    def do_yield(self, until):
        '''Hands control to the next task with work to do, will return as
        soon as there is time.'''
        self.wait_until(until, self.__yield_queue, None)

    def wait_until(self, until, suspend_queue, wakeup):
        '''The calling task is suspended.  If a deadline is given then the
        task will definitely be woken up when the deadline is reached if not
        before.  If a suspend_queue is given then the task is added to it
        (and it is the caller's responsibility to ensure the task is woken
        up, with a call to wakeup()).
            Returns True iff the wakeup is from a timeout.'''
        # If no wakeup has been specified, create one.  This is a key
        # component for ensuring consistent behaviour of the system: the
        # wakeup object ensures each task is only woken up exactly once.
        if wakeup is None:
            wakeup = self.__Wakeup(suspend_queue, until)
            
        # If a timeout or a suspension queue has been specified, add
        # ourselves as appropriate.  Failing either of these it's up to the
        # caller to arrange a wakeup.
        if suspend_queue is not None:
            suspend_queue.append(wakeup)
        if until is not None:
            self.__timer_queue.put(wakeup, until)

        # Suspend until we're woken.
        # Normally this call will return control to __tick(), but there are
        # two other cases to consider.  On the very first suspend control is
        # returned to the top of __scheduler(), and more interestingly, on
        # suspending immediately after calling poll_scheduler() control is
        # returned to __select().  This last case expects a list of ready
        # descriptors to be returned, so we have to be compatible with this!
        result = self.__greenlet.switch([])
        if result == _WAKEUP_INTERRUPT:
            # We get here if main is suspended and the scheduler decides
            # to die.  Make sure our wakeup is cancelled, and then
            # re-raise the offending exception.
            wakeup.wakeup(result)
            raise
        else:
            return result == _WAKEUP_TIMEOUT
            
    def poll_until(self, poller, until):
        '''Cooperative poll: the calling task is suspended until one of
        the specified waitable objects becomes ready or the timeout expires.
        '''
        # Add our poller to the appropriate poll event queues so that we'll
        # get woken.  Note that we don't need to worry about coming off the
        # queue: this'll be managed in _compute_poll_list later on
        poller.wakeup = self.__Wakeup(None, until)
        for file in poller.events:
            self.__poll_queue.setdefault(file, []).append(poller)
        # It's vital to yield during this call, even if we have actually
        # timed out -- otherwise the wakeup we've just added to the poll
        # queue will get processed when it's no longer valid (oops).
        self.wait_until(until, None, poller.wakeup)


    def __Wakeup(self, queue, until):
        if until is None:
            return _Wakeup(self.__wakeup_task, queue, None)
        else:
            return _Wakeup(self.__wakeup_task, queue, self.__timer_queue)

    def __wakeup_task(self, task, reason):
        if reason != _WAKEUP_INTERRUPT:
            self.__ready_queue.append((task, reason))
                
    def __wakeup_poll(self, poll_result):
        '''Called with the result of a system poll: a list of file descriptors
        and wakeup reasons.  Each waiting task is informed.'''
        # Work through all the notified files: with each file is a received
        # event mask which we'll pass through to the interested task.
        #
        # Some care is required here if we are to neither deliver spurious
        # wakeups nor lose wakeups.
        #     We make two assumption about our wakeup call, translating into
        # assumptions on either coselect.poll_block or poll_scheduler:
        #   1/ if an event is ready on a file we will eventually be notified;
        #   2/ if an event is not ready we will not be notified -- in other
        #      words, if a poll notify occurs we can safely access the file
        #      without risk of blocking.
        #
        # The goal of the loop below is to translate these assumptions into
        # corresponding properties on poll_until.  The problem arises when
        # there is more than one listener on an event, as the first listener
        # may consume the event before subsequent listeners receive it.
        #     The simplest solution is to be to communicate each event to just
        # one interested listener, but ensure that the event remains
        # monitored.
        for file, events in poll_result:
            for poller in self.__poll_queue.get(file, []):
                # Consume any events taken by the woken process
                events &= ~poller.notify_wakeup(file, events)


class Timedout(Exception):
    '''Waiting for event timed out.'''


def AbsTimeout(timeout):
    '''A timeout is represented in one of three forms:

    None            A timeout that never expires
    interval        A relative timeout interval
    (deadline,)     An absolute deadline

    This routine checks that the given input is in one of these three forms
    and returns a timeout in absolute deadline format.'''
    if timeout is None:
        return None
    elif isinstance(timeout, tuple):
        return timeout
    else:
        return (timeout + time.time(),)

def Deadline(timeout):
    '''Converts a timeout into a deadline.'''
    if timeout is None:
        return None
    else:
        return AbsTimeout(timeout)[0]
    

class EventBase(object):
    '''The base class for implementing events and signals.'''

    def __init__(self):
        # List of tasks currently waiting to be woken up.
        self.__wait_queue = _WakeupQueue()
        # Number of aborted waits that need to be emulated.  This is
        # incremented by subclasses for each _Wakeup that needs to be
        # simulated.
        self.__wait_abort = 0

    def _WaitUntil(self, timeout):
        '''Suspends the calling task until _Wakeup() is called.  Raises an
        exception if a timeout occurs first.'''
        deadline = Deadline(timeout)
        # If the deadline has already expired don't call into the scheduler:
        # as a matter of policy, we don't lose control in this case.
        # Otherwise the scheduler will tell us if we've timed out.
        if (deadline is not None and time.time() >= deadline) or \
                _scheduler.wait_until(deadline, self.__wait_queue, None):
            raise Timedout('Timed out waiting for event')

    def _Wakeup(self, wake_all):
        '''Wakes one or all waiting tasks.  Returns False if an aborted wait
        needs to be emulated.'''
        if self.__wait_abort and not wake_all:
            # This is a special case: an aborted wait needs to be completed.
            # This occurs when waiting needs to be simulated, in which case
            # any resources consumed by the reader need to be consumed by the
            # waker instead!
            self.__wait_abort -= 1
            return False
        else:
            self.__wait_queue.wake(wake_all)
            return True

    def _AbortWait(self):
        self.__wait_abort += 1

        

class Spawn(EventBase):
    '''This class is used to wrap cooperative threads: every task (except
    for main) managed by the scheduler should be an instance of this class.'''

    finished = property(fget = lambda self: bool(self.__result))
    
    def __init__(self, function, *args, **kargs):
        '''The given function and arguments will be called as a new task.
        All of the arguments will be be passed through to function, except for
        the special keyword raise_on_wait which defaults to False.
            If raise_on_wait is set then any exception raised during the
        execution of this task will be postponed until Wait() is called.  This
        allows such exceptions to be caught without disturbing the normal
        operation of the system.  Otherwise any exception is reported.'''
        EventBase.__init__(self)
        self.__function = function
        self.__args = args
        self.__kargs = kargs
        self.__result = ()
        self.__raise_on_wait = kargs.pop('raise_on_wait', False)
        # Hand control over to the run method in the scheduler.
        _scheduler.spawn(self.__run)

    def __run(self, _):
        try:
            # Try for normal successful result.
            self.__result = (True,
                self.__function(*self.__args, **self.__kargs))
        except:
            # Oops: the task terminated with an exception.  
            if self.__raise_on_wait:
                # The creator of the task is willing to catch this exception,
                # so hang onto it now until Wait() is called.
                self.__result = (False, sys.exc_info())
            else:
                # No good.  We can't allow this exception to propagate, as
                # doing so will kill the scheduler.  Instead report the
                # traceback right here.
                print 'Spawned task', \
                    getattr(self.__function, '__name__', '(unknown)'), \
                    'raised uncaught exception'
                traceback.print_exc()
                self.__result = (True, None)
        if not self._Wakeup(True):
            # Aborted wakeup: consume the result now
            del self.__result
        # See wait_until() for an explanation of this return value.
        return []

    def Wait(self, timeout = None):
        '''Waits until the task has completed.  May raise an exception if the
        task terminated with an exception and raise_on_wait was selected.
        Can only be called once, as the result is deleted after call.'''
        if not self.__result:
            self._WaitUntil(timeout)
        ok, result = self.__result
        # Delete the result before returning to avoid cycles: in particular,
        # if the result is an exception the associated traceback needs to be
        # dropped now.
        del self.__result
        if ok:
            return result
        else:
            # Re-raise the exception that actually killed the task here where
            # it can be received by whoever waits on the task.
            # There's a real reference count looping problem here -- can't
            # make the task go away when it's finished with...
            raise result[0], result[1], result[2]

    def AbortWait(self):
        '''Called instead of performing a proper wait to release any resources
        that might be consumed until the wait occurs.'''
        if self.__result:
            # Result has already arrived.  Consume it silently now.
            del self.__result
        else:
            # Still need to wait: need to abort the next wakeup.
            self._AbortWait()
            
    
class Event(EventBase):
    '''Any number of tasks can wait for an event to occur.  A single value
    can also be associated with the event.'''

    value = property(fget = lambda self: self.__value)
    
    def __init__(self, auto_reset = True):
        '''An event object is either signalled or reset.  Any task can wait
        for the object to become signalled, and it will be suspended until
        this occurs.  

        The intial value can be specified, as can the behaviour on succesfully
        signalling a process: if auto_reset=True is specified then only one
        task at a time sees any individual signal on this object.'''
        EventBase.__init__(self)
        self.__value = ()
        self.__auto_reset = auto_reset
        
    def __nonzero__(self):
        '''Tests whether the event is signalled.'''
        return bool(self.__value)
        
    def Wait(self, timeout = None):
        '''The caller will block until the event becomes true, or until the
        timeout occurs if a timeout is specified.  A Timeout exception is
        raised if a timeout occurs.'''
        # If one task resets the event while another is waiting the wait may
        # fail, so we have to loop here.
        deadline = AbsTimeout(timeout)
        while not self.__value:
            self._WaitUntil(deadline)

        ok, result = self.__value
        if self.__auto_reset:
            # If this is an auto reset event then we reset it on exit;
            # this means that we're the only thread that sees it being
            # signalled.  
            self.__value = ()

        # Finally return the result as a value or raise an exception.
        if ok:
            return result
        else:
            raise result

    def AbortWait(self):
        '''Called instead of performing a proper wait to release any resources
        that might be consumed until the wait occurs.'''
        # If this isn't an auto_reset event then our aborted wait makes no
        # difference.  Otherwise we either consume the value now or on the
        # next wakeup.
        if self.__auto_reset:
            if self.__value:
                self.Reset()
            else:
                self._AbortWait()
            
    def Signal(self, value = None):
        '''Signals the event.  Any waiting tasks are scheduled to be woken.'''
        self.__value = (True, value)
        if not self._Wakeup(not self.__auto_reset):
            self.Reset()

    def SignalException(self, exception):
        '''Signals the event with an exception: the next call to wait will
        receive an exception instead of a normal return value.'''
        self.__value = (False, exception)
        if not self._Wakeup(not self.__auto_reset):
            self.Reset()

    def Reset(self):
        '''Resets the event (and erases the value).'''
        self.__value = ()


class EventQueue(EventBase):
    '''A queue of objects.  A queue can also be treated as an iterator.'''

    def __init__(self):
        EventBase.__init__(self)
        self.__queue = []
        self.__closed = False

    def __len__(self):
        '''Returns the number of objects waiting on the queue.'''
        return len(self.__queue)

    def Wait(self, timeout = None):
        '''Returns the next object from the queue, or raises a Timeout
        exception if the timeout expires first.'''
        deadline = AbsTimeout(timeout)
        while not self.__queue and not self.__closed:
            self._WaitUntil(deadline)
        if self.__queue:
            return self.__queue.pop(0)
        else:
            raise StopIteration

    def AbortWait(self):
        '''Called instead of performing a proper wait to release any resources
        that might be consumed until the wait occurs.'''
        if self.__queue:
            self.__queue.pop(0)
        elif not self.__closed:
            self._AbortWait()
        
    def Signal(self, value):
        '''Adds the given value to the tail of the queue.'''
        assert not self.__closed, 'Can\'t write to a closed queue'
        self.__queue.append(value)
        if not self._Wakeup(False):
            self.__queue.pop(0)

    def close(self):
        '''An event queue can be closed.  This will cause waiting to raise
        the StopIteration exception (once existing entries have been read),
        and will prevent any further signals to the queue.'''
        self.__closed = True
        self._Wakeup(True)

    def __iter__(self):
        '''An event queue can itself be treated as an iterator: this allows
        event dispatching using a for loop, and provides some support for
        combining queues.'''
        return self

    def next(self):
        return self.Wait()


class ThreadedEventQueue(object):
    '''An event queue designed to work with threads.'''

    def __init__(self):
        # According to the documentation this is thread safe, so we don't
        # need to take any particular precautions when using this!
        self.__values = collections.deque()
        self.wait_descriptor, self.__signal = os.pipe()

    def Wait(self, timeout = None):
        '''Waits for a value to be written to the queue.  This can safely be
        called from either a cothread or another thread: the appropriate form
        of cooperative or normal blocking will be selected automatically.'''
        if thread.get_ident() == _scheduler_thread_id:
            # Normal cothread case, use cooperative wait
            poll = coselect.poll_list
        else:
            # Another thread, so block caller until ready
            poll = coselect.poll_block
        if not poll([(self.wait_descriptor, coselect.POLLIN)], timeout):
            raise Timedout('Timed out waiting for signal')
            
        os.read(self.wait_descriptor, 1)
        return self.__values.popleft()

    def Signal(self, value):
        '''Posts a value to the event queue.  This can safely be called from
        a thread or a cothread.'''
        self.__values.append(value)
        os.write(self.__signal, '-')


        
class Timer(object):
    '''A cancellable one-shot or auto-retriggering timer.'''
    
    def __init__(self, timeout, callback, retrigger = False):
        '''The callback will be called after the specified timeout.  If
        retrigger is set then the timer will automatically retrigger until
        it is cancelled.'''
        assert callable(callback), 'Ensure the callback is callable'
        self.__timeout = timeout
        self.__callback = callback
        self.__retrigger = retrigger
        self.__cancel = Event(auto_reset = False)
        Spawn(self.__timer)

    def __timer(self):
        while True:
            try:
                self.__cancel.Wait(self.__timeout)
            except Timedout:
                # There can be a race between cancelling and timing out:
                # ensure that if we were cancelled before being fired we do
                # nothing.
                if self.__cancel:
                    return
                self.__callback()
            if not self.__retrigger:
                return

    def cancel(self):
        '''Cancels the timer: the timer is guaranteed not to fire once this
        call has been made.'''
        self.__retrigger = False
        self.__cancel.Signal()
        del self.__callback

    def reset(self, timeout = None, retrigger = False):
        '''Resets the timer.  If it hasn't fired yet then the timeout is reset
        to the given timeout (or its original timeout by default).  ???
        '''
        assert False, 'Got to write this yet...'
            
            

def WaitForAll(event_list, timeout = None):
    '''Waits for all events in the event list to become ready or for the
    timeout to expire.'''
    # Make sure that the timeout is actually a deadline, then it's easy to do
    # all the waits in sequence.
    timeout = AbsTimeout(timeout)
    # Unfortunately our waiting can be interrupted by an exception.  To avoid
    # leaking memory in this case we perform simulated waits on any remaining
    # events.  This is a good deal more complicated than
    #       return [event.Wait(timeout) for event in event_list]
    # which is what it ought to be!
    event_list = list(event_list)
    result = []
    try:
        for n, event in enumerate(event_list):
            result.append(event.Wait(timeout))
    finally:
        for event in event_list[n+1:]:
            event.AbortWait()
    return result



# Other possibly desirable entites:
#
#   The ability to wait for an event to occur on one of a set of objects.
#       This would probably require quite deep hooking into the queueing
#       mechanism, and seems of limited value (the natural alternative is to
#       create a task per event).
#
#   The ability to kill a task
#       This is probably doable with the .throw greenlet method (or even with
#       a special wakeup value), but may require some care.



_QuitEvent = Event(auto_reset = False)

def Quit():
    '''Signals the quit event.  Once signalled it stays signalled.'''
    _QuitEvent.Signal()
    
def WaitForQuit(catch_interrupt = True):
    '''Waits for the quit event to be signalled.'''
    try:
        _QuitEvent.Wait()
    except KeyboardInterrupt:
        if catch_interrupt:
            # As a courtesy we quietly catch and discard the keyboard
            # interrupt.  Unfortunately we don't have full control over where
            # this is going to be caught, but if we get it we can exit
            # quietly.
            pass
        else:
            raise


# There is only the one scheduler, which we create right away.  A dedicated
# scheduler task is created: this allows the main task to suspend, but does
# mean that the scheduler is not the parent of all the tasks it's managing.
_scheduler = _Scheduler.create()
# We hang onto the thread ID for the cothread thread (at present there can
# only be one) so that we can recognise when we're in another thread.
_scheduler_thread_id = thread.get_ident()


def SleepUntil(deadline):
    '''Sleep until the specified deadline.  Note that if the deadline has
    already passed then no yield of control will occur.'''
    if deadline is None or time.time() < deadline:
        _scheduler.wait_until(deadline, None, None)

def Sleep(timeout):
    '''Sleep until the specified timeout has expired.'''
    SleepUntil(Deadline(timeout))

def Yield(timeout = 0):
    '''Hands control back to the scheduler.  Control is returned either after
    the specified timeout has passed, or as soon as there are no active jobs
    waiting to be run.'''
    _scheduler.do_yield(Deadline(timeout))
