# -*- coding: UTF-8 -*-

import copy
import os
import threading
import uuid
import allure
import six
import re
import imp
import time
import socket
import inspect
import traceback
import pytest
import json

from inspect import getfile
from selenium import webdriver
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.remote.remote_connection import RemoteConnection
from selenium.common.exceptions import WebDriverException
from http.client import BadStatusLine
from testtools.runtest import RunTest
from testtools import testcase
from unittest.suite import TestSuite
from unittest2.suite import TestSuite as TestSuite2

try:
    from urllib import parse
except ImportError:  # above is available in py3+, below is py2.7
    import urlparse as parse

from salmon import config
from salmon import debug
from salmon import entry
from salmon.logger import context
from salmon.logger import log as logging
from salmon.runner import exceptions
from salmon.runner.server import Remote
from salmon.screenshot import Screenshot
from salmon.tdata.lock import BaseLock as ResourceLock
from salmon.tdata import prefix_parse_data
from salmon.video import Video
from salmon.tdata import fresh_parse_data
from salmon.common import monkey_patch
from salmon.i18n import _

CONF = config.CONF

LOG = logging.getLogger(name=__name__)

monkey_patch.monkey_patch_traceback2()
monkey_patch.monkey_patch_repr_failure()

def monkey_patch_run_test():
    # To take screen shot when every exception occurs
    ORIGINAL_EXC_HANDLE = RunTest._got_user_exception

    def salmon_except_hook(self, exc_info, tb_label='traceback'):
        try:
            Screenshot.create(driver=Screenshot.driver)

        except Exception:
            LOG.error("Failed to take screen shot for exception: %s" % traceback.format_exc())

        return ORIGINAL_EXC_HANDLE(self, exc_info, tb_label)

    RunTest._got_user_exception = salmon_except_hook


def monkey_testsuite_teardown(test_suite):
    # To run teardownclass even when setupclass failed,
    # so the resources and browser can always be released
    ORIGINAL_TEARDOWN = test_suite._tearDownPreviousClass

    def teardown_hook(self, test, result):
        previousClass = getattr(result, '_previousTestClass', None)
        old_classSetupFailed = None
        if getattr(previousClass, '_classSetupFailed', False):
            old_classSetupFailed = previousClass._classSetupFailed
            previousClass._classSetupFailed = False
        try:
            return ORIGINAL_TEARDOWN(self, test, result)
        finally:
            if old_classSetupFailed:
                previousClass._classSetupFailed = old_classSetupFailed

    test_suite._tearDownPreviousClass = teardown_hook


def monkey_patch_remoteConnection_request():
    # To resolve the problem of browser connection is expired when 'keep_alive' is True
    ORIGINAL_REQUEST = RemoteConnection._request

    def request_hook(self, method, url, body=None):
        retry_time = 1
        retry_max = 2
        while True:
            try:
                return ORIGINAL_REQUEST(self, method, url, body)
            except BadStatusLine as e:
                if retry_time > retry_max:
                    raise RuntimeError("After %s retry, browser connection is still expired: %s" %
                                       (retry_max, e))
                LOG.info("Browser connection is expired, so let's try again(%s)"
                         "method: %s\nurl: %s\nbody: %s" % (retry_time, method, url, body))
                retry_time += 1

    RemoteConnection._request = request_hook


monkey_patch_run_test()
monkey_testsuite_teardown(TestSuite)
monkey_testsuite_teardown(TestSuite2)
monkey_patch_remoteConnection_request()
_teardowns = threading.local()

_ids = threading.local()
_ids.runner = None
_ids.runner = str(uuid.uuid4()) if not _ids.runner else _ids.runner

def setup_logger(fn):
    def _set_logger(self, **kwargs):
        self.test_name = "%s.%s.%s_%s" % (
            self.__module__, self.__class__.__name__, self._testMethodName,
            time.strftime("%Y-%m-%d_%H-%M-%S"))
        self.context = context.TestCaseContext(
            build_id=self.build_id, runner_id=self.runner_id,
            suite_id=self.suite_id, test_name=self.test_name)

        ret = fn(self, **kwargs)
        self.conf["context"] = self.context
        return ret

    return _set_logger
	
	
def fixture(fn):
    def wrapper(self, *args, **kwargs):
        if self._is_skip_all():
            LOG.info("All func are skiped, so don't execute prefix")
            return
        self.teardowns = []
        name = "%s.%s.%s_%s" % (self.__module__, self.__class__.__name__, fn.__name__,
                                time.strftime("%Y-%m-%d_%H-%M-%S"))
        self.context = context.TestCaseContext(
            build_id=self.build_id, runner_id=self.runner_id,
            suite_id=self.suite_id, test_name=name)

        imported = self._get_tdata_module(self.__class__)
        tdata = copy.deepcopy(self.__class__.tdata)
        fixture_tdata = getattr(imported, "tdata_" + fn.__name__)
        tdata.update(fixture_tdata)
        tdata = prefix_parse_data(tdata=tdata, pkg=self.__class__.pkg,
                                  class_search_filter=self.__class__.class_search_filter)
        self.tdata = tdata
        self.setup_screen_shot(name=name)
        self.setup_video(name=name)
        self.conf["context"] = self.context
        self.__class__.is_fixture = True
        fixture_teardown = {
            "teardown_name": name + "_teardown",
            "teardowns": [],
            "is_updated": False
        }
        _teardowns.exceptions = []
        self.__class__.fixture_teardowns.insert(0, fixture_teardown)
        try:
            ret = fn(self, *args, **kwargs)
            self.__class__.tdata = self.tdata
            return ret
        except Exception:
            Screenshot.create(self.driver)
            raise
        finally:
            self.__class__.is_fixture = False
            fixture_teardown.update({"is_updated": True})
            self.stop_record()

    return wrapper
	
def add_pytest_mark(type, f):
    getattr(pytest.mark, type)(f)
    getattr(pytest.mark, "all")(f)
    if type == "bvt":
        getattr(pytest.mark, "smoke")(f)
    elif type in ["level_1", "level_2", "level_3"]:
        getattr(pytest.mark, "func")(f)

    # TODO(Yanci) optimi
    from acmp_sailor.tests.function.module_helpler import get_module
    modules = get_module(f)
    for module in modules:
        getattr(pytest.mark, module)(f)

def add_attr(**kwargs):
    def decorator(f):

        if "type" in kwargs:
            case_type = kwargs.get("type")
            if isinstance(case_type, str):
                f = testcase.attr(case_type)(f)
                add_pytest_mark(case_type, f)
            elif isinstance(case_type, list):
                for attr in case_type:
                    f = testcase.attr(attr)(f)
                    add_pytest_mark(attr, f)
            else:
                raise TypeError("Test case type must be str or list not %s"
                                % type(case_type).__name__)
        else:
            raise ValueError("the params must include type")

        if "id" in kwargs:
            id = kwargs.get("id")
            if not isinstance(id, six.string_types):
                raise TypeError("Test case id must be string not %s"
                                % type(id).__name__)
            uuid.UUID(id)
            f = testcase.attr("id-%s" % id)(f)
            if f.__doc__:
                f.__doc__ = "Test idempotent id: %s\n%s" % (id, f.__doc__)
            else:
                f.__doc__ = "Test idempotent id: %s" % id
        else:
            raise ValueError("the params must include id")

        if "skip" in kwargs:
            LOG.info("skip the testcase(%s), the reason is %s" % (f.__name__, kwargs.get("skip")))
            f = testcase.attr("skip")(f)
            testcase.skip(kwargs.get("skip"))(f)

        return f

    return decorator
	
def add_level(**kwargs):
    """A decorator which applies the testtools attr decorator

    This decorator applies the testtools.testcase.attr if it is in the list of
    attributes to testtools we want to apply.
    """

    def decorator(f):
        if 'type' in kwargs and isinstance(kwargs['type'], str):
            attr = kwargs['type']
            f = testcase.attr(attr)(f)
            add_pytest_mark(attr, f)

        elif 'type' in kwargs and isinstance(kwargs['type'], list):
            for attr in kwargs['type']:
                f = testcase.attr(attr)(f)
                add_pytest_mark(attr, f)
        else:
            raise ValueError("The param(%s) must contain type" % kwargs)
        return f

    return decorator
	
def add_id(id):
    """Stub for metadata decorator"""
    if not isinstance(id, six.string_types):
        raise TypeError('Test idempotent_id must be string not %s'
                        '' % type(id).__name__)
    uuid.UUID(id)

    def decorator(f):
        f = testcase.attr('id-%s' % id)(f)
        if f.__doc__:
            f.__doc__ = 'Test idempotent id: %s\n%s' % (id, f.__doc__)
        else:
            f.__doc__ = 'Test idempotent id: %s' % id
        return f

    return decorator
	
class BaseTestCase(entry.TESTS):

    def __setattr__(self, key, value):
        self.__dict__[key] = value
        if key == "tdata":
            self._save_debug_file(tdata=self.tdata)

    def run(self, result=None):
        ret = super(BaseTestCase, self).run(result=result)
        if not self._is_skip_all():
            if hasattr(self, "tdata"):
                LOG.debug("after tdata=%s" % self.tdata)
            # if ret.wasSuccessful():
            #     LOG.info("=" * 20)
            #     LOG.info("SCENARIO SUCCESSFULLY")
            #     LOG.info("=" * 20)
            self.stop_record()
        today_str = re.findall(r"(\d{4}-\d{1,2}-\d{1,2})", self.test_name)[0]
        allure.attach(u"视频和截图地址", "%s/%s/%s"
                      % (CONF.video.ip_address, today_str, self.test_name))

    def stop_record(self):
        if Video.record:
            # Record video more 30 seconds in order to get more information
            if self._user_is_ci():
                time.sleep(30)
            else:
                time.sleep(5)
            Video.stop_record()

    @staticmethod
    def _get_tdata_module(cls):
        test_file_path = getfile(cls)
        tdata_file_name = re.sub(r"^test", "tdata", os.path.basename(test_file_path))
        tdata_file_name = re.sub(r"\.pyc$", ".py", tdata_file_name)
        tdata_file_path = os.path.join(os.path.dirname(test_file_path), tdata_file_name)
        return imp.load_source(cls.__name__, tdata_file_path)

    @staticmethod
    def _get_tdata_debug_file(cls):
        test_file_path = getfile(cls)
        tdata_file_name = re.sub(r"\.pyc$", ".py", os.path.basename(test_file_path))
        tdata_file_name = re.sub(r"\.py$", ".json", tdata_file_name)
        tdata_file_path = os.path.join(os.path.dirname(test_file_path), tdata_file_name)
        return tdata_file_path
	
	@staticmethod
    def _insert_pkg_version_limit(resource_filter, type, version):
        for flt in resource_filter:
            if resource_filter[flt]["category"] == type:
                resource_filter[flt]["descriptor.version"] = version

    @classmethod
    def _add_env_version_limit(cls, resource_filter):
        acloud_version = CONF.env_version.acloud
        acmp_verson = CONF.env_version.acmp
        if acloud_version:
            cls._insert_pkg_version_limit(resource_filter, "CLUSTER", acloud_version)
        if acmp_verson:
            cls._insert_pkg_version_limit(resource_filter, "ACMP", acmp_verson)

    @classmethod
    def _user_is_ci(cls):
        return True if os.environ.get("JOB_NAME") else False

    @classmethod
    def _get_user(cls):
        if cls._user_is_ci():
            file_path = inspect.getabsfile(cls)
            if "test" in file_path:
                file_path = file_path.partition("tests")[-1]
                file_path = file_path[1:]  # cut of the '/' or '\'
            file_path = re.sub(r"/|\\", ".", file_path)
            file_path = file_path.partition(".py")[0]

            return "%(job)s_%(num)s_%(test)s_%(time)s" % {
                "job": os.environ["JOB_NAME"],
                "num": os.environ["BUILD_NUMBER"],
                "test": "%s.%s" % (file_path, cls.__name__),
                "time": time.strftime("%Y-%m-%d_%H:%M:%S")}
        else:
            return socket.gethostname()

    @classmethod
    def setup_load_tdata(cls):
        # if debug then read tmp json file
        if cls._is_debug() and debug.DebugSwitch.TDATA_DEBUG:
            LOG.debug("Is Debug")
            if cls._read_debug_file():
                LOG.debug("Read Debug File Success")
                return
        LOG.debug("Is Not Debug")
        tdata = cls._get_tdata_module(cls).tdata
        tdata = copy.deepcopy(tdata)
        resource_filter = tdata["SALMON_RESOURCE"]
        cls._add_env_version_limit(resource_filter)
        cls.res_username = cls._get_user()
        if cls._user_is_ci():
            timeout = 14400
        else:  # user is local
            timeout = 30
        resource_locked = ResourceLock.run_lock(resource_filter=resource_filter,
                                                user=cls.res_username,
                                                rule_map=cls.rule_map,
                                                timeout=timeout)
        LOG.debug("get resource successfully")
        tdata["SALMON_RESOURCE"] = resource_locked
        cls.tdata = prefix_parse_data(tdata=tdata, pkg=cls.pkg,
                                      class_search_filter=cls.class_search_filter)
        # cls._save_debug_file()
		
	@classmethod
    def _save_debug_file(cls, tdata=None):
        if tdata is None:
            tdata = cls.tdata
        debug_json_path = cls._get_tdata_debug_file(cls)
        with open(debug_json_path, "w") as f:
            f.write(json.dumps(tdata))

    @classmethod
    def _read_debug_file(cls):
        ret = False
        debug_json_path = cls._get_tdata_debug_file(cls)
        try:
            with open(debug_json_path, "r") as f:
                data = f.read()
                cls.tdata = json.loads(data)
                ret = True
        except Exception, e:
            LOG.warning("read debug path = %s" % debug_json_path)
        return ret

    @classmethod
    def _is_debug(cls):
        ret = False
        try:
            oracle_vars = dict(
                (a, b) for a, b in os.environ.items() if a.find('IPYTHONENABLE') >= 0)
            if len(oracle_vars) > 0:
                ret = True
        except ZeroDivisionError as e:
            LOG.debug("Is Not Debug %s" % e.message)
        return ret

    @classmethod
    def _release_resource(cls):
        if hasattr(cls, 'tdata'):
            resource_locked = cls.tdata.get("SALMON_RESOURCE")
            if resource_locked:
                if cls._user_is_ci():
                    ResourceLock.run_unlock(resource_locked=resource_locked, user=cls.res_username)
                    LOG.debug("Resources are unlocked")
                else:
                    LOG.info("Because the user is local, there is no need to unlock")
	
	
	@classmethod
    def _is_skip_all(cls):
        funcs = []
        attrs = dir(cls)
        for attr in attrs:
            if re.search("^test_", attr):
                funcs.append(attr)
        skip_count = 0
        for func in funcs:
            fn = getattr(cls, func)
            if hasattr(fn, "__testtools_attrs"):
                if "skip" in getattr(fn, "__testtools_attrs"):
                    skip_count += 1
        if skip_count == len(funcs):
            return True
        return False

    @classmethod
    def setUpClass(cls):
        cls.conf = {}
        cls.build_id = cls.bid
        cls.runner_id = _ids.runner
        cls.suite_id = str(uuid.uuid4())
        cls.module_class_name = "%s.%s" % (cls.__module__, cls.__name__)
        cls.context = context.TestCaseContext(
            build_id=cls.build_id, runner_id=cls.runner_id,
            suite_id=cls.suite_id, test_name=cls.module_class_name)

        if cls._is_skip_all():
            LOG.info("All func are skiped, so don't execute setUpClass")
            return

        super(BaseTestCase, cls).setUpClass()

        cls.setup_load_tdata()
        try:
            cls.REMOTE_SERVERS = []
            if debug.DebugSwitch.LOCAL_DEBUG:
                server = debug.DebugSwitch.LOCAL_DEBUG_SERVER
            elif debug.DebugSwitch.REMOTE_DEBUG:
                server = debug.DebugSwitch.REMOTE_DEBUG_SERVER
            else:
                server = cls.get_remote_server(user=cls.res_username)
                cls.REMOTE_SERVERS.append(server)
            cls.CURRENT_SERVER = server
            cls.CURRENT_CAPABILITIES = cls.set_capabilities()
            cls._init_driver()
        except Exception:
            cls._release_resource()
            for server in cls.REMOTE_SERVERS:
                cls.release_remote_server(server)
            raise

        cls.fixture_teardowns = []

    def load_tdata(self):
        # TODO fix me
        if self._is_debug() and debug.DebugSwitch.TDATA_DEBUG:
            if self._read_debug_file():
                if len(self.__class__.tdata) > 1:  # 是否一定要等于3
                    LOG.debug("Debug before tdata=%s" % self.tdata)
                    return
        imported = self._get_tdata_module(self.__class__)
        setup_tdata = copy.deepcopy(self.__class__.tdata)
        test_tdata = getattr(imported,
                             self._testMethodName.replace("test", "tdata"))
        setup_tdata.update(test_tdata)
        tdata = setup_tdata
        tdata = prefix_parse_data(tdata=tdata, pkg=self.__class__.pkg,
                                  class_search_filter=self.__class__.class_search_filter)
        self.tdata = tdata
        self._save_debug_file(self.tdata)
        LOG.debug("before tdata=%s" % self.tdata)

    @setup_logger
    def setUp(self):
        super(BaseTestCase, self).setUp()
        _teardowns.exceptions = []
        # self.driver = None
        self.teardowns = []
        self.load_tdata()
        self.setup_screen_shot()
        self.setup_video()
	
	
	    def setup_screen_shot(self, name=None):
        if name is None:
            time_pattern = r"(_\d{4}-\d{1,2}-\d{1,2}_\d{1,2}-\d{1,2}-\d{1,2})"
            name = re.sub(time_pattern, "", self.test_name)

        Screenshot.test_name = name

    def setup_video(self, name=None):
        server = self.CURRENT_SERVER
        Video.server_host = server.get('bind_host')
        Video.vnc_port = server.get('vnc_bind_port')

        if name is None:
            name = self.test_name
        Video.test_name = name

        Video.start_record()

    @classmethod
    def set_capabilities(cls):
        browser_type = cls.get_browser_type()
        browser_version = cls.get_browser_version()
        if browser_type == "chrome":
            capabilities = DesiredCapabilities.CHROME.copy()
            capabilities["chromeOptions"] = {
                "args": ["--disable-infobars", "--ignore-certificate-errors",
                         "--allow-running-insecure-content", "--disable-web-security"]
            }
        elif browser_type == "firefox":
            capabilities = DesiredCapabilities.FIREFOX.copy()
        elif browser_type == "ie":
            raise NotImplementedError()
        else:
            raise Exception("not supported browser")

        capabilities["acceptSslCerts"] = True
        return capabilities

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "driver"):
            cls.driver.quit()
            LOG.debug("Browser is closed")

        if cls._is_skip_all():
            LOG.info("All func are skiped, so don't execute tearDownClass")
            return

        cls._release_resource()

        for server in cls.REMOTE_SERVERS:
            cls.release_remote_server(server)
        LOG.debug("Remote browser server is released")

        super(BaseTestCase, cls).tearDownClass()
	
	
	@classmethod
    def _init_driver(cls):
        server = cls.CURRENT_SERVER["url"]
        capabilities = cls.CURRENT_CAPABILITIES

        retry = 3
        cls.driver = None
        for i in range(retry):
            try:
                cls.driver = webdriver.Remote(
                    command_executor=server,
                    desired_capabilities=capabilities,
                    keep_alive=True)
                break
            except WebDriverException as e:
                LOG.warning("Create browser failed(%s): %s" % (type(e), e))

        if not cls.driver:
            raise RuntimeError("Failed to create browser after %s retry" % retry)
        cls.driver.set_window_size(0, 0)
        cls.driver.maximize_window()

        Screenshot.driver = cls.driver

        return cls.driver

    def _init_conf(self):
        return self.conf

    def teardown(self):
        if self.driver:
            window_handles = self.driver.window_handles
            current_window_handle = self.driver.current_window_handle
            for window_handle in window_handles:
                if window_handle != current_window_handle:
                    self.driver.switch_to.window(window_handle)
                    self.driver.close()
                    self.driver.switch_to.window(current_window_handle)

        if hasattr(_teardowns, "exceptions"):
            if len(_teardowns.exceptions) > 0:
                LOG.error("Exceptions raised when teardown:")
                for e in _teardowns.exceptions:
                    LOG.error("*" * 20 + "Teardown Exception" + "*" * 20)
                    LOG.error(e)

                raise ValueError(_("Exceptions raised when teardown"))

    def tearDown(self):
        self.teardown()
        super(BaseTestCase, self).tearDown()

    def _teardown_fresh_data(self, selector=None, param_name=None, param_type=None):
        kwargs = dict()
        config = fresh_parse_data(selector=selector, tdata=self.tdata).values()[0]
        if param_type is dict:
            data = {param_name: config}
        elif param_type is list:
            data = {param_name: [config]}
        else:
            raise ValueError("Now, the param_type(%s) must be one of [list, dict]"
                             % param_type)
        kwargs.update(data)
        return kwargs
		
	    def _teardown(self, teardowns=None):
        for teardown in teardowns:
            client_str = teardown.get("client")
            func_str = teardown.get("func")
            kwargs = teardown.get("kwargs")
            params = teardown.get("params")
            try:
                client = self.__getattribute__(client_str)
                func = client.__getattribute__(func_str)

                if kwargs:
                    pass
                if params:
                    if not kwargs:
                        kwargs = {}
                    for param in params:
                        is_eixst_selector = param.get("is_eixst_selector", True)
                        selector = param.get("selector")
                        param_name = param.get("param_name")
                        param_type = param.get("param_type", list)
                        value = param.get("value")
                        if is_eixst_selector:
                            if not selector:
                                raise ValueError("selector are not set in teardown. "
                                                 "client:%(client)s, func:%(func)s" %
                                                 {"client": client_str, "func": func_str})
                            else:
                                data = self._teardown_fresh_data(selector, param_name, param_type)
                                kwargs.update(data)
                        else:
                            if not value:
                                raise ValueError("There is no selector, the param of value "
                                                 "must be assiged")
                            else:
                                kwargs.update(
                                    {
                                        param_name: value
                                    }
                                )

                extra_params = ["login_type", "username", "password"]
                for param in extra_params:
                    if param in kwargs.keys():
                        kwargs.pop(param)

                ret = func(**kwargs)

            except Exception as e:
                Screenshot.create(self.driver, "exception occured when teardown (%s.%s, %s)")
                _teardowns.exceptions.append(e)
                LOG.error(u"Teardown Exceptions:(%s.%s, %s) %s\n%s" %
                          (client_str, func_str, kwargs, e, traceback.format_exc().decode("utf-8")))
                continue

    def teardown_append(self, client=None, func=None, params=None, **kwargs):
        """

        :param client:
        :param func:
        :param params:
         [
            {
                "param_name": "xxx"
                "selector": "XXXX"
            }
         ]
        :param kwargs:
        :return:
        """
        td = self._teardown_append(client=client, func=func, params=params, **kwargs)
        self.teardowns.insert(0, td)

    def _teardown_append(self, client=None, func=None, params=None, **kwargs):
        td = {
            "client": client,
            "func": func,
            "kwargs": kwargs,
        }
        if params:
            td.update(
                {
                    "params": params,
                    "kwargs": kwargs
                }
            )
        return td
	
	@classmethod
    def get_browser_type(cls):
        """
        get the type of the web browser
        :return: ie10|ie11|chrome|firefox
        """
        if debug.DebugSwitch.LOCAL_DEBUG or debug.DebugSwitch.REMOTE_DEBUG:
            browser_type = cls.CURRENT_SERVER.get("type", None)
            if browser_type is None:
                raise Exception("The browser_type is not specified "
                                "in DEBUG model")
            return browser_type

        browser_type = os.getenv(CONF.environment_name.test_browser_type, None)
        browser_type = "chrome" if not browser_type else browser_type
        if browser_type is None:
            raise exceptions.BrowserTypeNotSpecifiedException(
                "The browser type is not specified")

        if browser_type not in CONF.browser.supported_type:
            raise exceptions.BrowserTypeNotSupportedException(
                "the browser type (%s) is not supported" % browser_type)

        return browser_type

    @classmethod
    def get_browser_version(cls):
        if debug.DebugSwitch.LOCAL_DEBUG or debug.DebugSwitch.REMOTE_DEBUG:
            version = cls.CURRENT_SERVER.get("version", None)
        else:
            version = os.getenv(CONF.environment_name.test_browser_version, None)
        return version

    @classmethod
    def get_remote_server(cls, user=None):
        """
        get the remote standalone server which is  used for test
        :return:
        """
        browser_type = cls.get_browser_type()
        version = cls.get_browser_version()
        server = Remote.get_server(browser_type=browser_type,
                                   browser_version=version,
                                   user=user)
        if server is None:
            raise exceptions.NoneRemoteServerAllocated("there is 0 remote "
                                                       "server allocated")
        return server

    @staticmethod
    def release_remote_server(server):

        ret = Remote.release_server(server)
        if not ret:
            raise exceptions.ReleaseRemoteServerException()