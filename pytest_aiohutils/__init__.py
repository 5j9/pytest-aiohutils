__version__ = '0.23.0'
import atexit
from collections.abc import Callable, Mapping
from inspect import iscoroutinefunction
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

from aiohutils.session import ClientSession, SessionManager
from pydantic import ConfigDict
from pydantic.type_adapter import TypeAdapter
from pytest import Config, Function, Parser, StashKey, fixture

# Define a Stash Key for storing calculated configuration (Pytest idiomatic way)
CONFIG_KEY = StashKey[dict]()


def pytest_addoption(parser: Parser):
    """Registers the command line and ini-file options."""
    group = parser.getgroup('testconfig')
    group.addoption(
        '--record',
        action='store_true',
        default=False,
        dest='RECORD_MODE',
        help='Enable record mode for tests (saves new responses).',
    )
    group.addoption(
        '--online',
        action='store_true',
        default=False,
        dest='ONLINE_MODE',
        help='Force tests to run online (disables mocking).',
    )
    group.addoption(
        '--remove-unused-data',
        action='store_true',
        default=False,
        dest='REMOVE_UNUSED_TESTDATA',
        help='Remove test data files not used during the run.',
    )


_remove_unused_testdata: bool
testdata: Path
tests_path: Path


def pytest_configure(config: Config):
    """Called after command line options are parsed and configuration is loaded."""
    global tests_path, _remove_unused_testdata, testdata

    RECORD_MODE = config.getoption('RECORD_MODE')
    ONLINE_MODE = config.getoption('ONLINE_MODE')

    # It is offline UNLESS (RECORD_MODE is True OR ONLINE_MODE is True)
    OFFLINE_MODE = not (RECORD_MODE or ONLINE_MODE)

    REMOVE_UNUSED_TESTDATA = _remove_unused_testdata = (
        config.getoption('REMOVE_UNUSED_TESTDATA') and OFFLINE_MODE
    )

    # Store calculated config in the Pytest config stash
    config.stash[CONFIG_KEY] = {
        'RECORD_MODE': RECORD_MODE,
        'OFFLINE_MODE': OFFLINE_MODE,
        'REMOVE_UNUSED_TESTDATA': REMOVE_UNUSED_TESTDATA,
    }

    # Set the tests_path to the root of the test directory (e.g., /project_root/tests),
    tests_path = Path(config.rootpath) / 'tests'
    testdata = tests_path / 'testdata'

    if _remove_unused_testdata:
        atexit.register(remove_unused_testdata)


class TestConfig(TypedDict):
    RECORD_MODE: bool
    OFFLINE_MODE: bool
    REMOVE_UNUSED_TESTDATA: bool
    TESTS_PATH: Path


@fixture(scope='session')
def test_config(request) -> TestConfig:
    """Provides test configuration variables to test functions."""
    # Retrieve configuration from the Pytest Stash using the standard 'request' fixture
    config_data = request.config.stash.get(CONFIG_KEY, {})

    # Fallback in case fixture is called before pytest_configure for some reason
    return {
        'RECORD_MODE': config_data.get('RECORD_MODE', False),
        'OFFLINE_MODE': config_data.get('OFFLINE_MODE', True),
        'REMOVE_UNUSED_TESTDATA': config_data.get(
            'REMOVE_UNUSED_TESTDATA', False
        ),
        'TESTS_PATH': tests_path,
    }


class EqualToEverything:
    """A placeholder object that always evaluates as equal."""

    def __eq__(self, other):
        return True


class FakeResponse:
    """A mock response object for offline mode."""

    __slots__ = 'file'
    url = EqualToEverything()
    history = ()

    def __init__(self, file: Path) -> None:
        self.file = file

    async def read(self) -> bytes:
        return self.file.read_bytes()

    def raise_for_status(self):
        pass

    async def text(self) -> str:
        return (await self.read()).decode()


class FakeSession:
    __slots__ = ()
    file_map: list[tuple[str, Path | list[Path]]] = []
    url_to_key: Callable[[str], str]

    @classmethod
    def file(cls, url: str):
        print(url, cls.file_map)
        for url_end, file_or_files in cls.file_map:
            if url.endswith(url_end):
                break
        else:
            raise ValueError('URL did not match any file_map entries.')

        if isinstance(file_or_files, list):
            file = file_or_files.pop()
        else:
            file = file_or_files

        return file

    async def request(self, method: str, url: str, *_, **__):
        file = self.file(url)
        _used_files.add(file)
        return FakeResponse(file)


@fixture(scope='session')
# DEPENDENCY INJECTION: session now depends on test_config to get its mode
async def session(test_config: TestConfig):
    """Pytest fixture to mock or record HTTP sessions."""
    # Use injected config variables instead of globals
    RECORD_MODE = test_config['RECORD_MODE']
    OFFLINE_MODE = test_config['OFFLINE_MODE']

    if OFFLINE_MODE:
        orig_session = SessionManager.session
        SessionManager.session = FakeSession()  # type: ignore
        yield
        SessionManager.session = orig_session  # type: ignore
        return

    if RECORD_MODE:
        original_request = ClientSession.request

        async def recording_request(*args, **kwargs):
            resp = await original_request(*args, **kwargs)
            content = await resp.read()
            FakeSession.file(args[2]).write_bytes(content)
            return resp

        ClientSession.request = recording_request  # type: ignore

        yield
        ClientSession.request = original_request
        return

    # If not OFFLINE and not RECORD, just run with the original session (live online)
    yield
    return


def pytest_collection_modifyitems(items: list[Function]):
    """Automatically apply the 'session' fixture to all async tests."""
    for item in items:
        if iscoroutinefunction(item.obj):
            item.fixturenames.append('session')


def remove_unused_testdata():
    """Removes test data files that were not used during the test run."""
    unused_testdata = {*testdata.iterdir()} - _used_files

    if not unused_testdata:
        print('REMOVE_UNUSED_TESTDATA: no action required')
        return
    for filename in unused_testdata:
        (testdata / filename).unlink()
        print(f'REMOVE_UNUSED_TESTDATA: removed {filename}')


_used_files = set[Path]()
# atexit.register is now called conditionally in pytest_configure


def file(filename: str):
    return patch.object(
        FakeSession,
        'file_map',
        [('', testdata / filename)],
    )


def files(*filenames: str):
    return patch.object(
        FakeSession,
        'file_map',
        [('', [testdata / fn for fn in reversed(filenames)])],
    )


def file_map(*url_end__filename: tuple[str, str]):
    return patch.object(
        FakeSession,
        'file_map',
        [
            (key, testdata / file_name)
            for (key, file_name) in url_end__filename
        ],
    )


strict_config = ConfigDict(strict=True)


def validate_dict(dct: Mapping, typed_dct: type[TypedDict]):  # type: ignore
    # A trick to disallow extra keys. See
    # https://stackoverflow.com/questions/77165374/runtime-checking-for-extra-keys-in-typeddict
    # https://docs.pydantic.dev/2.4/concepts/strict_mode/#dataclasses-and-typeddict
    typed_dct.__pydantic_config__ = strict_config  # type: ignore
    TypeAdapter(typed_dct).validate_python(dct, strict=True)
