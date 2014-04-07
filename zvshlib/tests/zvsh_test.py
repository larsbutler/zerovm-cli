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

from collections import OrderedDict
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
            puts=0,
            put_size=0,
        )
        exp = ('Channel = '
               '/dev/stdin,/dev/stdin,0,0,4294967296,4294967296,0,0')
        assert exp == str(chan)

    def test_read_channel(self):
        chan = zvsh.Channel(
            '/dev/stdin',
            '/dev/stdin',
            0,
            gets=1024,
            get_size=2048,
            puts=0,
            put_size=0,
        )
        exp = ('Channel = '
               '/dev/stdin,/dev/stdin,0,0,1024,2048,0,0')
        assert exp == str(chan)

    def test_write_channel_with_defaults(self):
        # Typical representation for a stdout write channel.
        # Uses defaults wherever possible.
        chan = zvsh.Channel(
            '/tmp/zvsh/stdout.1',
            '/dev/stdout',
            0,
            0,
            gets=0,
            get_size=0,
        )
        exp = ('Channel = '
               '/tmp/zvsh/stdout.1,/dev/stdout,0,0,0,0,4294967296,4294967296')
        assert exp == str(chan)

    def test_write_channel(self):
        chan = zvsh.Channel(
            '/tmp/zvsh/stdout.1',
            '/dev/stdout',
            0,
            0,
            gets=0,
            get_size=0,
            puts=256,
            put_size=128,
        )
        exp = ('Channel = '
               '/tmp/zvsh/stdout.1,/dev/stdout,0,0,0,0,256,128')
        assert exp == str(chan)

    def test_repr(self):
        chan = zvsh.Channel(
            '/dev/stdin',
            '/dev/stdin',
            0,
            puts=0,
            put_size=0,
        )
        exp = ('<Channel = '
               '/dev/stdin,/dev/stdin,0,0,4294967296,4294967296,0,0>')
        assert exp == repr(chan)


class TestManifest:
    """
    Tests for :class:`zvshlib.zvsh.Manifest`.
    """

    def test_default_manifest(self):
        io_lim = 4294967296
        expected = """\
Node = 1
Version = 20130611
Timeout = 50
Memory = 4294967296,0
Program = /tmp/zvsh/boot.1
Channel = /dev/stdin,/dev/stdin,0,0,%(io_lim)s,%(io_lim)s,0,0
Channel = /tmp/zvsh/stdout.1,/dev/stdout,0,0,0,0,%(io_lim)s,%(io_lim)s
Channel = /tmp/zvsh/stderr.1,/dev/stderr,0,0,0,0,%(io_lim)s,%(io_lim)s
Channel = /tmp/zvsh/nvram.1,/dev/nvram,3,0,%(io_lim)s,%(io_lim)s,%(io_lim)s,\
%(io_lim)s"""
        expected %= dict(io_lim=io_lim)

        man = zvsh.Manifest.default_manifest('/tmp/zvsh', '/tmp/zvsh/boot.1')
        assert expected == man.dumps()

    def test_default_manifest_with_custom_channel(self):
        io_lim = 4294967296
        expected = """\
Node = 1
Version = 20130611
Timeout = 50
Memory = 4294967296,0
Program = /tmp/zvsh/boot.1
Channel = /dev/stdin,/dev/stdin,0,0,%(lim)s,%(lim)s,0,0
Channel = /tmp/zvsh/stdout.1,/dev/stdout,0,0,0,0,%(lim)s,%(lim)s
Channel = /tmp/zvsh/stderr.1,/dev/stderr,0,0,0,0,%(lim)s,%(lim)s
Channel = \
/tmp/zvsh/nvram.1,/dev/nvram,3,0,%(lim)s,%(lim)s,%(lim)s,%(lim)s
Channel = \
/path/to/python.tar,/dev/5.python.tar,3,0,%(lim)s,%(lim)s,%(lim)s,%(lim)s"""
        expected %= dict(lim=io_lim)

        man = zvsh.Manifest.default_manifest('/tmp/zvsh', '/tmp/zvsh/boot.1')
        python_chan = zvsh.Channel(
            '/path/to/python.tar',
            '/dev/5.python.tar',
            zvsh.RND_READ_RND_WRITE,
        )
        man.channels.append(python_chan)
        assert expected == man.dumps()

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
                            zvsh.RND_READ_RND_WRITE, gets=2048, get_size=2048,
                            puts=2048, put_size=2048)
        man = zvsh.Manifest('20130611', 10, 1024, '/tmp/zvsh/boot.1',
                            channels=[chan])
        assert expected == man.dumps()

    def test_manifest_no_channels(self):
        # If there are no channels, an error should be raised.
        man = zvsh.Manifest('20130611', 10, 1024, '/tmp/zvsh.boot.1')
        with pytest.raises(RuntimeError):
            man.dumps()


class TestNVRAM:
    """
    Tests for :class:`zvshlib.zvsh.NVRAM`.
    """

    def setup_method(self, _method):
        self.prog_args = ['app.nexe', '-c', 'print "hello, world"']
        self.processed_images = [
            ('/home/user1/usr.tar', '/usr', 'ro'),
            ('/home/user1/etc.tar', '/etc', 'rw'),
            ('/home/user1/tmp.tar', '/tmp', 'ro'),
        ]
        self.env_dict = OrderedDict([('PATH', '/bin:/usr/bin'),
                                     ('LANG', 'en_US.UTF-8,'),
                                     ('TERM', 'vt100')])

    def test_dumps(self):
        nvram = zvsh.NVRAM(self.prog_args,
                           processed_images=self.processed_images,
                           env=self.env_dict,
                           debug_verbosity=4)

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
verbosity=4
""")
        with mock.patch('sys.stdin.isatty') as stdin:
            with mock.patch('sys.stdout.isatty') as stdout:
                with mock.patch('sys.stderr.isatty') as stderr:
                    stdin.return_value = True
                    stdout.return_value = True
                    stderr.return_value = True
                    assert nvram.dumps() == expected

    def test_dumps_no_images(self):
        # We would encounter this case when we are executing a nexe directly
        # with zvsh/zerovm (without mounting any tar images as a file system).
        nvram = zvsh.NVRAM(self.prog_args,
                           processed_images=None,
                           env=self.env_dict,
                           debug_verbosity=4)

        expected = (
            r"""[args]
args = app.nexe -c print\x20\x22hello\x2c\x20world\x22
[fstab]

[mapping]
channel=/dev/stdin,mode=char
channel=/dev/stdout,mode=char
channel=/dev/stderr,mode=char
[env]
name=PATH,value=/bin:/usr/bin
name=LANG,value=en_US.UTF-8\x2c
name=TERM,value=vt100
[debug]
verbosity=4
""")
        with mock.patch('sys.stdin.isatty') as stdin:
            with mock.patch('sys.stdout.isatty') as stdout:
                with mock.patch('sys.stderr.isatty') as stderr:
                    stdin.return_value = True
                    stdout.return_value = True
                    stderr.return_value = True
                    assert nvram.dumps() == expected


def test_create_manifest():
    # Test for :func:`zvhslib.zvsh.create_manifest`.
    working_dir = tempfile.mkdtemp()
    program_path = '%s/boot.2' % working_dir
    manifest_cfg = dict(Node=2, Version='20130611', Timeout=100, Memory=1024)
    tar_files = ['/usr/share/foo.tar', 'bar.tar']
    limits_cfg = dict(reads=16, rbytes=32, writes=64, wbytes=128)

    expected_manifest_text = """\
Node = 2
Version = 20130611
Timeout = 100
Memory = 1024,0
Program = %(wd)s/boot.2
Channel = /dev/stdin,/dev/stdin,0,0,4294967296,4294967296,0,0
Channel = %(wd)s/stdout.1,/dev/stdout,0,0,0,0,4294967296,4294967296
Channel = %(wd)s/stderr.1,/dev/stderr,0,0,0,0,4294967296,4294967296
Channel = \
%(wd)s/nvram.1,/dev/nvram,3,0,4294967296,4294967296,4294967296,4294967296
Channel = /usr/share/foo.tar,/dev/1.foo.tar,3,0,16,32,64,128
Channel = %(wd)s/bar.tar,/dev/2.bar.tar,3,0,16,32,64,128"""

    expected_manifest_text %= dict(wd=working_dir)

    try:
        old_wd = os.getcwd()
        os.chdir(working_dir)

        manifest = zvsh.create_manifest(working_dir, program_path,
                                        manifest_cfg, limits_cfg,
                                        tar_files=tar_files)

        assert manifest.dumps() == expected_manifest_text
    finally:
        os.chdir(old_wd)
        shutil.rmtree(working_dir)


def test__check_runtime_files():
    # Test for :func:`zvshlib.zvsh._check_runtime_files`.
    _, file_a = tempfile.mkstemp()
    _, file_b = tempfile.mkstemp()
    os.unlink(file_b)
    files = dict(a=file_a, b=file_b)

    # A case where 1 of the files already exists:
    with pytest.raises(RuntimeError):
        zvsh._check_runtime_files(files)

    # A case where none of the files exist:
    os.unlink(file_a)
    zvsh._check_runtime_files(files)
