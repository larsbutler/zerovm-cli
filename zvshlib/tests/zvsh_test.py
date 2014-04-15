#  Copyright 2014 Rackspace, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import mock
import os
import pytest
import shutil
import tempfile

try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6 fallback
    from ordereddict import OrderedDict

from copy import copy
from zvshlib import zvsh


class TestChannel:
    """
    Tests for :class:`zvshlib.zvsh.Channel`.
    """

    def test_read_channel_with_defaults(self):
        # Simple string representation for a typical stdin read channel.
        chan = zvsh.Channel(
            '/dev/stdin',
            '/dev/stdin',
            0,
            writes=0,
            wbytes=0,
        )
        exp = ('Channel = '
               '/dev/stdin,/dev/stdin,0,0,4294967296,4294967296,0,0')
        assert exp == chan.to_manifest()

    def test_read_channel(self):
        chan = zvsh.Channel(
            '/dev/stdin',
            '/dev/stdin',
            0,
            reads=1024,
            rbytes=2048,
            writes=0,
            wbytes=0,
        )
        exp = ('Channel = '
               '/dev/stdin,/dev/stdin,0,0,1024,2048,0,0')
        assert exp == chan.to_manifest()

    def test_write_channel_with_defaults(self):
        # Typical representation for a stdout write channel.
        # Uses defaults wherever possible.
        chan = zvsh.Channel(
            '/tmp/zvsh/stdout.1',
            '/dev/stdout',
            0,
            0,
            reads=0,
            rbytes=0,
        )
        exp = ('Channel = '
               '/tmp/zvsh/stdout.1,/dev/stdout,0,0,0,0,4294967296,4294967296')
        assert exp == chan.to_manifest()

    def test_write_channel(self):
        chan = zvsh.Channel(
            '/tmp/zvsh/stdout.1',
            '/dev/stdout',
            0,
            0,
            reads=0,
            rbytes=0,
            writes=256,
            wbytes=128,
        )
        exp = ('Channel = '
               '/tmp/zvsh/stdout.1,/dev/stdout,0,0,0,0,256,128')
        assert exp == chan.to_manifest()

    def test_repr(self):
        chan = zvsh.Channel(
            '/dev/stdin',
            '/dev/stdin',
            0,
            writes=0,
            wbytes=0,
        )
        exp = ('<Channel = '
               '/dev/stdin,/dev/stdin,0,0,4294967296,4294967296,0,0>')
        assert exp == repr(chan)

    def test_to_nvram_fstab(self):
        chan = zvsh.Channel('/tmp/foo.py', '/dev/1.foo.py', 3)

        assert ('channel=/dev/1.foo.py,mountpoint=/,access=ro,removable=no'
                == chan.to_nvram_fstab())

    def test_to_nvram_mapping(self):
        chan = zvsh.Channel('/tmp/foo.py', '/dev/1.foo.py', 3)

        assert 'channel=/dev/1.foo.py,mode=file' == chan.to_nvram_mapping()


class TestManifest:
    """
    Tests for :class:`zvshlib.zvsh.Manifest`.
    """

    def test_manifest(self):
        # Generate a minimal manifest, with just 1 channel.
        io_lim = 2048
        expected = """\
Node = 1
Version = 20130611
Timeout = 10
Memory = 1024,0
Program = /tmp/zvsh/boot.1
Channel = \
/path/to/foo.tar,/dev/foo.tar,3,0,%(lim)s,%(lim)s,%(lim)s,%(lim)s"""
        expected %= dict(lim=io_lim)
        chan = zvsh.Channel('/path/to/foo.tar', '/dev/foo.tar',
                            zvsh.RND_READ_RND_WRITE, reads=2048, rbytes=2048,
                            writes=2048, wbytes=2048)
        man = zvsh.Manifest('/tmp/zvsh/boot.1',
                            channels=[chan],
                            timeout=10,
                            memory=1024)
        assert expected == man.dumps()

    def test_manifest_no_channels(self):
        # If there are no channels, an error should be raised.
        man = zvsh.Manifest('/tmp/zvsh.boot.1', [])
        with pytest.raises(RuntimeError):
            man.dumps()


class TestNVRAM:
    """
    Tests for :class:`zvshlib.zvsh.NVRAM`.
    """

    def setup_method(self, _method):
        self.prog_args = ['app.nexe', '-c', 'print "hello, world"']
        self.channels = [
            zvsh.Channel('usr.tar', '/dev/1.usr.tar', 3, access='ro',
                         mount_dir='/usr', is_image=True),
            zvsh.Channel('etc.tar', '/dev/2.etc.tar', 3, access='rw',
                         mount_dir='/etc', is_image=True),
            zvsh.Channel('tmp.tar', '/dev/3.tmp.tar', 3, access='ro',
                         mount_dir='/tmp', is_image=True),

            zvsh.Channel('/dev/stdin', '/dev/stdin', 0, mode='char',
                         is_image=False),
            zvsh.Channel('/dev/stdout', '/dev/stdout', 0, mode='char',
                         is_image=False),
            zvsh.Channel('/dev/stderr', '/dev/stderr', 0, mode='char',
                         is_image=False),
        ]
        self.env_dict = OrderedDict([('PATH', '/bin:/usr/bin'),
                                     ('LANG', 'en_US.UTF-8,'),
                                     ('TERM', 'vt100')])

    def test_dumps(self):
        nvram = zvsh.NVRAM(self.prog_args,
                           self.channels,
                           env=self.env_dict,
                           debug_verbosity=3)

        expected = (
            r"""[args]
args = app.nexe -c print\x20\x22hello\x2c\x20world\x22
[fstab]
channel=/dev/1.usr.tar,mountpoint=/usr,access=ro,removable=no
channel=/dev/2.etc.tar,mountpoint=/etc,access=rw,removable=no
channel=/dev/3.tmp.tar,mountpoint=/tmp,access=ro,removable=no
[mapping]
channel=/dev/stdin,mode=char
channel=/dev/stdout,mode=char
channel=/dev/stderr,mode=char
[env]
name=PATH,value=/bin:/usr/bin
name=LANG,value=en_US.UTF-8\x2c
name=TERM,value=vt100
[debug]
verbosity=3
""")
        assert nvram.dumps() == expected


def test__get_manifest():
    # Test for :func:`zvhslib.zvsh._get_manifest`.
    working_dir = tempfile.mkdtemp()
    program_path = '%s/boot.2' % working_dir
    manifest_cfg = dict(Node=2, Version='20130611', Timeout=100, Memory=1024)
    channels = [
        zvsh.Channel('/dev/stdin', '/dev/stdin', 0, mode='char',
                     is_image=False, writes=0, wbytes=0),
        zvsh.Channel('%s/stdout.2' % working_dir, '/dev/stdout', 0,
                     mode='char', is_image=False, reads=0, rbytes=0),
        zvsh.Channel('%s/stderr.2' % working_dir, '/dev/stderr', 0,
                     mode='char', is_image=False, reads=0, rbytes=0),
        zvsh.Channel('/usr/share/foo.tar', '/dev/1.foo.tar', 3, access='ro',
                     is_image=True),
        zvsh.Channel('%s/bar.tar' % working_dir, '/dev/2.bar.tar', 3,
                     access='rw', is_image=True),
    ]

    expected_manifest_text = """\
Node = 2
Version = 20130611
Timeout = 100
Memory = 1024,0
Program = %(wd)s/boot.2
Channel = /dev/stdin,/dev/stdin,0,0,4294967296,4294967296,0,0
Channel = %(wd)s/stdout.2,/dev/stdout,0,0,0,0,4294967296,4294967296
Channel = %(wd)s/stderr.2,/dev/stderr,0,0,0,0,4294967296,4294967296
Channel = /usr/share/foo.tar,/dev/1.foo.tar,3,0,4294967296,4294967296,\
4294967296,4294967296
Channel = %(wd)s/bar.tar,/dev/2.bar.tar,3,0,4294967296,4294967296,4294967296,\
4294967296"""

    expected_manifest_text %= dict(wd=working_dir)

    try:
        old_wd = os.getcwd()
        os.chdir(working_dir)

        manifest = zvsh._get_manifest(program_path, channels, manifest_cfg)

        assert manifest.dumps() == expected_manifest_text
    finally:
        os.chdir(old_wd)
        shutil.rmtree(working_dir)


def test__get_nvram():
    # Test for :func:`zvshlib.zvsh._get_nvram`.
    working_dir = '/tmp'
    channels = [
        zvsh.Channel('/dev/stdin', '/dev/stdin', 0, mode='char',
                     is_image=False, writes=0, wbytes=0),
        zvsh.Channel('%s/stdout.2' % working_dir, '/dev/stdout', 0,
                     mode='char', is_image=False, reads=0, rbytes=0),
        zvsh.Channel('%s/stderr.2' % working_dir, '/dev/stderr', 0,
                     mode='char', is_image=False, reads=0, rbytes=0),
        zvsh.Channel('/usr/share/foo.tar', '/dev/2.foo.tar', 3, is_image=True),
        zvsh.Channel('%s/bar.tar' % working_dir, '/dev/3.bar.tar', 3,
                     access='rw', is_image=True),
        zvsh.Channel('%s/baz.log' % working_dir, '/dev/1.baz.log', 3),
    ]
    zvargs = mock.Mock()
    zvargs.args.command = 'foo.nexe'
    zvargs.processed_cmd_args = ['bar', '/dev/1.baz.log']
    # env vars from the command line
    zvargs.env = OrderedDict([
        ('FOO', 'bar'),
        ('_BAZ1', '\\blarg" ,'),
    ])
    # env vars from the config file (zvsh.cfg)
    env = OrderedDict([
        ('LANG', 'en_US.UTF-8'),
    ])

    nvram = zvsh._get_nvram(zvargs, channels, env=env)

    expected_nvram = r"""[args]
args = foo.nexe bar /dev/1.baz.log
[fstab]
channel=/dev/2.foo.tar,mountpoint=/,access=ro,removable=no
channel=/dev/3.bar.tar,mountpoint=/,access=rw,removable=no
[mapping]
channel=/dev/stdin,mode=char
channel=/dev/stdout,mode=char
channel=/dev/stderr,mode=char
channel=/dev/1.baz.log,mode=file
[env]
name=LANG,value=en_US.UTF-8
name=FOO,value=bar
name=_BAZ1,value=\x5cblarg\x22\x20\x2c
"""

    assert nvram.dumps() == expected_nvram


def test_prepare_channels():
    # Test for :func:`zvshlib.zvsh.prepare_channels`
    old_cwd = os.getcwd()
    working_dir = tempfile.mkdtemp()
    node = 2

    limits = dict(
        reads=16,
        rbytes=32,
        writes=64,
        wbytes=128,
    )

    zvargs = mock.Mock()
    zvargs.args.cmd_args = ['python', 'foo.py', '@bar.png', '@baz.log']

    zvargs.args.zvm_image = ['../python.tar,/share,rw',
                             'myapp.tar,/usr/lib',
                             'somelib.tar']
    zvargs.channels = [
        zvsh.Channel('%s/bar.png' % working_dir,
                     '/dev/2.bar.png',
                     3,
                     **limits),
        zvsh.Channel('%s/baz.log' % working_dir,
                     '/dev/3.baz.log',
                     3,
                     **limits),
        zvsh.Channel(os.path.abspath('%s/../python.tar' % working_dir),
                     '/dev/4.python.tar',
                     3,
                     access='rw',
                     mount_dir='/share',
                     **limits),
        zvsh.Channel('%s/myapp.tar' % working_dir,
                     '/dev/5.myapp.tar',
                     3,
                     mount_dir='/usr/lib',
                     **limits),
        zvsh.Channel('%s/somelib.tar' % working_dir,
                     '/dev/6.somelib.tar',
                     3,
                     **limits),
    ]

    stdin_chan = zvsh.Channel('/dev/stdin', '/dev/stdin', 0, mode='char',
                              reads=16, rbytes=32, writes=0, wbytes=0,
                              is_image=False)
    stdout_chan = zvsh.Channel('%s/stdout.2' % working_dir, '/dev/stdout', 0,
                               mode='char', reads=0, rbytes=0, writes=64,
                               wbytes=128, is_image=False)
    stderr_chan = zvsh.Channel('%s/stderr.2' % working_dir, '/dev/stderr', 0,
                               mode='char', reads=0, rbytes=0, writes=64,
                               wbytes=128, is_image=False)

    fstab_chan = zvsh.Channel('/home/user1/default.tar', '/dev/1.default.tar',
                              3, **limits)

    # Expected manifest channels
    exp_man_chans = [
        stdin_chan,
        stdout_chan,
        stderr_chan,
        fstab_chan,
        zvsh.Channel('%s/bar.png' % working_dir,
                     '/dev/2.bar.png',
                     3,
                     **limits),
        zvsh.Channel('%s/baz.log' % working_dir,
                     '/dev/3.baz.log',
                     3,
                     **limits),
        zvsh.Channel(os.path.abspath('%s/../python.tar' % working_dir),
                     '/dev/4.python.tar',
                     3,
                     access='rw',
                     mount_dir='/share',
                     **limits),
        zvsh.Channel('%s/myapp.tar' % working_dir,
                     '/dev/5.myapp.tar',
                     3,
                     mount_dir='/usr/lib',
                     **limits),
        zvsh.Channel('%s/somelib.tar' % working_dir,
                     '/dev/6.somelib.tar',
                     3,
                     **limits),
    ]
    # Expected nvram channels
    exp_nvram_chans = copy(exp_man_chans)

    try:
        os.chdir(working_dir)
        with mock.patch('zvshlib.zvsh._get_fd_mode') as fd_mode:
            fd_mode.side_effect = ['block', 'pipe', 'char', None, None, None]

            with mock.patch('sys.stdin') as stdin:
                with mock.patch('sys.stdout') as stdout:
                    with mock.patch('sys.stderr') as stderr:
                        # Case 1:
                        # stdin, stdout, and stderr are tyys
                        #  - include in manifest
                        #  - include in nvram, mode == 'char'
                        stdin.isatty.return_value = True
                        stdout.isatty.return_value = True
                        stderr.isatty.return_value = True

                        manifest_channels, nvram_channels = (
                            zvsh.prepare_channels(zvargs, working_dir, node,
                                                  limits,
                                                  fstab_channels=[fstab_chan])
                        )
                        assert exp_man_chans == manifest_channels
                        assert exp_nvram_chans == nvram_channels

                        # Case 2:
                        # stdin, stdout, and stderr are files
                        #  - include in manifest
                        #  - include in nvram, mode == 'file'
                        stdin.isatty.return_value = False
                        stdin_chan.mode = 'block'
                        stdout.isatty.return_value = False
                        stdout_chan.mode = 'pipe'
                        stderr.isatty.return_value = False
                        stderr_chan.mode = 'char'

                        manifest_channels, nvram_channels = (
                            zvsh.prepare_channels(zvargs, working_dir, node,
                                                  limits,
                                                  fstab_channels=[fstab_chan])
                        )
                        assert exp_man_chans == manifest_channels
                        assert exp_nvram_chans == nvram_channels

                        # Case 3:
                        # stdin, stdout, and stderr are not files or ttys
                        #  - include in manifest
                        #  - do not include in nvram
                        stdin_chan.mode = 'file'
                        stdout_chan.mode = 'file'
                        stderr_chan.mode = 'file'
                        exp_nvram_chans.remove(stdin_chan)
                        exp_nvram_chans.remove(stdout_chan)
                        exp_nvram_chans.remove(stderr_chan)

                        manifest_channels, nvram_channels = (
                            zvsh.prepare_channels(zvargs, working_dir, node,
                                                  limits,
                                                  fstab_channels=[fstab_chan])
                        )
                        assert exp_man_chans == manifest_channels
                        assert exp_nvram_chans == nvram_channels
    finally:
        shutil.rmtree(working_dir)
        os.chdir(old_cwd)


class TestZvArgs:
    """
    Tests for :class:`zvshlib.zvsh.ZvArgs`.
    """

    def test_process_args(self):
        limits = dict(
            reads=16,
            rbytes=32,
            writes=64,
            wbytes=128,
        )
        working_dir = tempfile.mkdtemp()
        old_cwd = os.getcwd()

        zvargs = zvsh.ZvArgs()
        zvargs.args = mock.Mock()
        zvargs.args.cmd_args = ['@../foo.txt', '@USERNAME=admin',
                                '@/tmp/bar.txt', 'baz.txt',
                                '@blarg.txt', 'test', '@_PASSWORD=secret']
        zvargs.args.zvm_image = ['../python.tar,/share,rw',
                                 'myapp.tar,/usr/lib', 'somelib.tar']

        exp_proc_cmd_args = ['/dev/1.foo.txt', '/dev/2.bar.txt', 'baz.txt',
                             '/dev/3.blarg.txt', 'test']
        exp_channels = [
            zvsh.Channel(os.path.abspath('%s/../foo.txt' % working_dir),
                         '/dev/1.foo.txt',
                         3,
                         **limits),
            zvsh.Channel('/tmp/bar.txt', '/dev/2.bar.txt', 3, **limits),
            zvsh.Channel('%s/blarg.txt' % working_dir, '/dev/3.blarg.txt', 3,
                         **limits),
            zvsh.Channel(os.path.abspath('%s/../python.tar' % working_dir),
                         '/dev/4.python.tar',
                         3,
                         access='rw',
                         mount_dir='/share',
                         is_image=True,
                         **limits),
            zvsh.Channel('%s/myapp.tar' % working_dir,
                         '/dev/5.myapp.tar',
                         3,
                         mount_dir='/usr/lib',
                         is_image=True,
                         **limits),
            zvsh.Channel('%s/somelib.tar' % working_dir,
                         '/dev/6.somelib.tar',
                         3,
                         is_image=True,
                         **limits),
        ]
        exp_env = OrderedDict([('USERNAME', 'admin'), ('_PASSWORD', 'secret')])

        try:
            os.chdir(working_dir)

            zvargs.process_args(limits)

            assert exp_proc_cmd_args == zvargs.processed_cmd_args
            assert exp_channels == zvargs.channels
            assert exp_env == zvargs.env
        finally:
            shutil.rmtree(working_dir)
            os.chdir(old_cwd)


class TestCreateFstabChannels:
    """
    Tests for :func:`_create_fstab_channels`.
    """

    def setup_method(self, _method):
        self.limits = dict(
            reads=1,
            rbytes=2,
            writes=4,
            wbytes=8,
        )

    def test_empty_config(self):
        zvconfig = {'fstab': {}}

        exp_chans = []
        act_chans = zvsh._create_fstab_channels(zvconfig, self.limits)

        assert exp_chans == act_chans

    def test(self):
        zvconfig = {'fstab': OrderedDict([
            ('/home/user1/python.tar', '/ ro'),
            ('/tmp/foo.tar', '/usr/share\trw'),
        ])}
        exp_chans = [
            zvsh.Channel('/home/user1/python.tar',
                         '/dev/1.python.tar',
                         3,
                         access='ro',
                         mount_dir='/',
                         is_image=True,
                         **self.limits),
            zvsh.Channel('/tmp/foo.tar',
                         '/dev/2.foo.tar',
                         3,
                         access='rw',
                         mount_dir='/usr/share',
                         is_image=True,
                         **self.limits),
        ]
        act_chans = zvsh._create_fstab_channels(zvconfig, self.limits)

        assert exp_chans == act_chans
