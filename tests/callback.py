#!/usr/bin/env dls-python2.6

# Test Callback mechanism

from __future__ import print_function

import require
import cothread
import time
import thread
import numpy


THREADS = 5

signal = cothread.EventQueue()

def do_signal(name, n):
    signal.Signal((name, n))

def signaller(name):
    n = 0
    while True:
        n += 1
        cothread.Callback(do_signal, name, n)
        time.sleep(0.1 * numpy.random.random())

for n in range(THREADS):
    thread.start_new_thread(signaller, ('Thread %d' % n,))

while True:
    name, n = signal.Wait(1)
    print('got', name, n)
