__version__ = '0.22.5.dev1'
import atexit
from collections.abc import Iterator, Mapping
from inspect import iscoroutinefunction
from itertools import cycle
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
        '--offline',
        action='store_true',
        default=True,
        dest='OFFLINE_MODE',
        help='Enable offline mode (only uses saved data).',
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
    # Note: If RECORD_MODE is true, we always disable OFFLINE_MODE
    OFFLINE_MODE = config.getoption('OFFLINE_MODE') and not RECORD_MODE
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
    # using Path objects for clean, cross-platform path construction.
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

    __slots__ = 'files'
    files: Iterator
    url = EqualToEverything()
    history = ()

    @property
    def file(self) -> Path:
        return next(self.files)

    async def read(self) -> bytes:
        return self.file.read_bytes()

    def raise_for_status(self):
        pass

    async def text(self) -> str:
        return (await self.read()).decode()


@fixture(scope='session')
# DEPENDENCY INJECTION: session now depends on test_config to get its mode
async def session(test_config: TestConfig):
    """Pytest fixture to mock or record HTTP sessions."""
    # Use injected config variables instead of globals
    RECORD_MODE = test_config['RECORD_MODE']
    OFFLINE_MODE = test_config['OFFLINE_MODE']

    if OFFLINE_MODE:

        class FakeSession:
            @staticmethod
            async def get(*_, **__):
                return FakeResponse()

        orig_session = SessionManager.session
        SessionManager.session = FakeSession()  # type: ignore
        yield
        SessionManager.session = orig_session  # type: ignore
        return

    if RECORD_MODE:
        original_get = ClientSession.get

        async def recording_get(*args, **kwargs):
            resp = await original_get(*args, **kwargs)
            content = await resp.read()
            FakeResponse().file.write_bytes(content)
            return resp

        ClientSession.get = recording_get  # type: ignore

        yield
        ClientSession.get = original_get
        return

    yield
    return


def pytest_collection_modifyitems(items: list[Function]):
    """Automatically apply the 'session' fixture to all async tests."""
    for item in items:
        if iscoroutinefunction(item.obj):
            item.fixturenames.append('session')


def remove_unused_testdata():
    """Removes test data files that were not used during the test run."""
    unused_testdata = {f.name for f in testdata.iterdir()} - USED_FILENAMES

    if not unused_testdata:
        print('REMOVE_UNUSED_TESTDATA: no action required')
        return
    for filename in unused_testdata:
        (testdata / filename).unlink()
        print(f'REMOVE_UNUSED_TESTDATA: removed {filename}')


USED_FILENAMES = set()
# atexit.register is now called conditionally in pytest_configure


def file(filename: str):
    """Mocks the response files for a single file test case."""
    # Checks if cleanup is active using the minimal global flag set in pytest_configure
    if _remove_unused_testdata:
        USED_FILENAMES.add(filename)

    return patch.object(
        FakeResponse,
        'files',
        cycle([testdata / filename]),
    )


def files(*filenames: str):
    """Mocks the response files for sequential file test cases."""
    # Checks if cleanup is active using the minimal global flag set in pytest_configure
    if _remove_unused_testdata:
        for filename in filenames:
            USED_FILENAMES.add(filename)

    return patch.object(
        FakeResponse,
        'files',
        (testdata / filename for filename in filenames),
    )


strict_config = ConfigDict(strict=True)


def validate_dict(dct: Mapping, typed_dct: type[TypedDict]):  # type: ignore
    # A trick to disallow extra keys. See
    # https://stackoverflow.com/questions/77165374/runtime-checking-for-extra-keys-in-typeddict
    # https://docs.pydantic.dev/2.4/concepts/strict_mode/#dataclasses-and-typeddict
    typed_dct.__pydantic_config__ = strict_config  # type: ignore
    TypeAdapter(typed_dct).validate_python(dct, strict=True)
