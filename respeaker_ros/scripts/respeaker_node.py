#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Author: furushchev <furushchev@jsk.imi.i.u-tokyo.ac.jp>

import angles
from contextlib import contextmanager
import usb.core
import usb.util
import pyaudio
import math
import numpy as np
import tf.transformations as T
import os
import rospy
import struct
import sys
import time
from audio_common_msgs.msg import AudioData
from geometry_msgs.msg import PoseStamped
from std_msgs.msg import Bool, Int32, ColorRGBA
from dynamic_reconfigure.server import Server

# https://stackoverflow.com/questions/21367320/searching-for-equivalent-of-filenotfounderror-in-python-2
try:
    FileNotFoundError
except NameError:
    FileNotFoundError = IOError
try:
    from pixel_ring import usb_pixel_ring_v2
except (IOError, FileNotFoundError) as e:
    rospy.logerr(e)
    raise RuntimeError("Check the device is connected and recognized")

try:
    from respeaker_ros.cfg import RespeakerConfig
except Exception as e:
    rospy.logerr(e)
    raise RuntimeError("Need to run respeaker_gencfg.py first")

from respeaker_ros import *

# suppress error messages from ALSA
# https://stackoverflow.com/questions/7088672/pyaudio-working-but-spits-out-error-messages-each-time
# https://stackoverflow.com/questions/36956083/how-can-the-terminal-output-of-executables-run-by-python-functions-be-silenced-i
@contextmanager
def ignore_stderr(enable=True):
    if enable:
        devnull = None
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            stderr = os.dup(2)
            sys.stderr.flush()
            os.dup2(devnull, 2)
            try:
                yield
            finally:
                os.dup2(stderr, 2)
                os.close(stderr)
        finally:
            if devnull is not None:
                os.close(devnull)
    else:
        yield


class RespeakerNode(object):
    def __init__(self):
        rospy.on_shutdown(self.on_shutdown)
        self.update_rate = rospy.get_param("~update_rate", 10.0)
        self.sensor_frame_id = rospy.get_param("~sensor_frame_id", "respeaker_base")
        self.doa_xy_offset = rospy.get_param("~doa_xy_offset", 0.0)
        self.doa_yaw_offset = rospy.get_param("~doa_yaw_offset", 90.0)
        self.speech_prefetch = rospy.get_param("~speech_prefetch", 0.5)
        self.speech_continuation = rospy.get_param("~speech_continuation", 0.5)
        self.speech_max_duration = rospy.get_param("~speech_max_duration", 7.0)
        self.speech_min_duration = rospy.get_param("~speech_min_duration", 0.1)
        suppress_pyaudio_error = rospy.get_param("~suppress_pyaudio_error", True)
        #
        self.respeaker = RespeakerInterface()
        self.speech_audio_buffer = b""
        self.is_speeching = False
        self.speech_stopped = rospy.Time(0)
        self.prev_is_voice = None
        self.prev_doa = None
        # advertise
        self.pub_vad = rospy.Publisher("is_speeching", Bool, queue_size=1, latch=True)
        self.pub_doa_raw = rospy.Publisher("sound_direction", Int32, queue_size=1, latch=True)
        self.pub_doa = rospy.Publisher("sound_localization", PoseStamped, queue_size=1, latch=True)
        self.pub_audio = rospy.Publisher("audio", AudioData, queue_size=10)
        self.pub_speech_audio = rospy.Publisher("speech_audio", AudioData, queue_size=10)
        # init config
        self.config = None
        self.dyn_srv = Server(RespeakerConfig, self.on_config)
        # start
        self.respeaker_audio = RespeakerAudio(self.on_audio, suppress_error=suppress_pyaudio_error)
        self.speech_prefetch_bytes = int(
            self.speech_prefetch * self.respeaker_audio.rate * self.respeaker_audio.bitdepth / 8.0)
        self.speech_prefetch_buffer = b""
        self.respeaker_audio.start()
        self.info_timer = rospy.Timer(rospy.Duration(1.0 / self.update_rate),
                                      self.on_timer)
        self.timer_led = None
        self.sub_led = rospy.Subscriber("status_led", ColorRGBA, self.on_status_led)

    def on_shutdown(self):
        self.info_timer.shutdown()
        try:
            self.respeaker.close()
        except:
            pass
        finally:
            self.respeaker = None
        try:
            self.respeaker_audio.stop()
        except:
            pass
        finally:
            self.respeaker_audio = None

    def on_config(self, config, level):
        if self.config is None:
            # first get value from device and set them as ros parameters
            for name in config.keys():
                config[name] = self.respeaker.read(name)
        else:
            # if there is different values, write them to device
            for name, value in config.items():
                prev_val = self.config[name]
                if prev_val != value:
                    self.respeaker.write(name, value)
        self.config = config
        return config

    def on_status_led(self, msg):
        self.respeaker.set_led_color(r=msg.r, g=msg.g, b=msg.b, a=msg.a)
        if self.timer_led and self.timer_led.is_alive():
            self.timer_led.shutdown()
        self.timer_led = rospy.Timer(rospy.Duration(3.0),
                                       lambda e: self.respeaker.set_led_trace(),
                                       oneshot=True)

    def on_audio(self, data):
        self.pub_audio.publish(AudioData(data=data))
        if self.is_speeching:
            if len(self.speech_audio_buffer) == 0:
                self.speech_audio_buffer = self.speech_prefetch_buffer
            self.speech_audio_buffer += data
        else:
            self.speech_prefetch_buffer += data
            self.speech_prefetch_buffer = self.speech_prefetch_buffer[-self.speech_prefetch_bytes:]

    def on_timer(self, event):
        stamp = event.current_real or rospy.Time.now()
        is_voice = self.respeaker.is_voice()
        doa_rad = math.radians(self.respeaker.direction - 180.0)
        doa_rad = angles.shortest_angular_distance(
            doa_rad, math.radians(self.doa_yaw_offset))
        doa = int(math.degrees(doa_rad))

        # vad
        if is_voice != self.prev_is_voice:
            self.pub_vad.publish(Bool(data=is_voice))
            self.prev_is_voice = is_voice

        # doa
        if doa != self.prev_doa:
            self.pub_doa_raw.publish(Int32(data=doa))
            self.prev_doa = doa

            msg = PoseStamped()
            msg.header.frame_id = self.sensor_frame_id
            msg.header.stamp = stamp
            ori = T.quaternion_from_euler(math.radians(doa), 0, 0)
            msg.pose.position.x = self.doa_xy_offset * np.cos(doa_rad)
            msg.pose.position.y = self.doa_xy_offset * np.sin(doa_rad)
            msg.pose.orientation.w = ori[0]
            msg.pose.orientation.x = ori[1]
            msg.pose.orientation.y = ori[2]
            msg.pose.orientation.z = ori[3]
            self.pub_doa.publish(msg)

        # speech audio
        if is_voice:
            self.speech_stopped = stamp
        if stamp - self.speech_stopped < rospy.Duration(self.speech_continuation):
            self.is_speeching = True
        elif self.is_speeching:
            buf = self.speech_audio_buffer
            self.speech_audio_buffer = b""
            self.is_speeching = False
            duration = 8.0 * len(buf) * self.respeaker_audio.bitwidth
            duration = duration / self.respeaker_audio.rate / self.respeaker_audio.bitdepth
            rospy.loginfo("Speech detected for %.3f seconds" % duration)
            if self.speech_min_duration <= duration < self.speech_max_duration:

                self.pub_speech_audio.publish(AudioData(data=buf))


if __name__ == '__main__':
    rospy.init_node("respeaker_node")
    n = RespeakerNode()
    rospy.spin()
