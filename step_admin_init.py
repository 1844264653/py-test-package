#! -*- coding: utf-8 -*-

import re
import time
import allure

from salmon.logger import log as logging
from selenium.webdriver.remote.webelement import WebElement
from selenium.common.exceptions import TimeoutException, WebDriverException

from acmp_sailor.step import StepBase
from sm_acmp.ui.components import notify_bubble_ok
from sm_acmp.ui.pages.admin import login as admin_login
from sm_acmp.ui.pages.admin.operate.application import history as admin_history, \
    ing as admin_ing, proc_mgt as admin_proc_mgt
from sm_acmp.ui.pages.org.operate.application import proc_mgt as org_proc_mgt, ing as org_ing, \
    history as org_history, mine as org_mine
from sm_acmp.ui.pages.admin import home as admin_home
from sm_acmp.ui.pages.user import application as user_order
from sm_acmp.ui.pages.user import cloud_host as user_cloud_host
from sm_acmp.ui.pages.user import management as user_management
from sm_acmp.ui.pages.admin.common import navigation
from sm_acmp.ui.pages.admin.resource import cloud_host, guide
from sm_acmp.ui.pages.admin.resource.pool import cluster
from sm_acmp.ui.pages.admin.resource.pool.cloud_resource import zone, cloud_environment
from sm_acmp.ui.pages.admin.resource.image import public, nfv, private
from sm_acmp.ui.pages.admin.management import system_config
from sm_acmp.ui.pages.admin.management.system_ops import upgrade
from sm_acmp.ui.pages.admin.management.system_config.serial_number import acmp as sn_acmp, \
    cluster as sn_cluster, nfv as sn_nfv
from sm_acmp.ui.pages.admin.monitor import alert_config, monitor_overview, alert_log
from sm_acmp.ui.pages.admin.management.business_ops import recycle_bin
from sm_acmp.ui.pages.admin.operate.user import user, role, organization, manager
from sm_acmp.ui.pages.admin.management import system_ops
from sm_acmp.ui.pages.admin.operate.charge import consumption as admin_consumption
from sm_acmp.ui.pages.admin.operate import charge
from sm_acmp.ui.pages.admin.resource.network.topology.org_vrouter.configure import subnet, \
    static_route, access_control, dns
from sm_acmp.ui.pages.admin.resource.network.topology.org_vrouter.configure import intranetdns as \
    topo_innerdns
from sm_acmp.ui.pages.admin.resource.network import topology, network_deployment
from sm_acmp.ui.pages.admin.resource.network import eip_pool
from sm_acmp.ui.pages.admin.operate.quota import table as quota_table
from sm_acmp.ui.pages.common import task_log
from sm_acmp.ui.pages.admin.resource.network import privateline
from sm_acmp.ui.pages.admin.resource.network.topology import org_privateline
from sm_acmp.api.admin.resource.network import privateline as privateline_api

from sm_acmp.config import env
from sm_acmp.ui.pages.org.operate.user import role as org_role
from sm_acmp.ui.pages.org import home as org_home
from sm_acmp.ui.pages.org.services.monitor_manage.quota import table as org_quota_table

# -------------------------api-------------------------------
from sm_acmp.api.admin.operate.user import organization as org_api
from sm_acmp.api.admin.operate.user import role as role_api
from sm_acmp.api.admin.resource import cloud_host as cloud_host_api
from sm_acmp.api.admin.resource.network import eip_pool as eip_pool_api
from sm_acmp.api.admin.resource.network.topo import domain as domain_api
from salmon.tdata import fresh_parse_data

LOG = logging.getLogger(name=__name__)


class StepAdmin(StepBase):
    ELEMENT_CLICK_METHOD = WebElement.click

    def setUp(self):
        super(StepAdmin, self).setUp()
        current_login_info = self.env_info.current_login_info
        host = current_login_info.get("host")
        port = current_login_info.get("port")
        http_protocal = "https"
        url = "{protocal}://{host}:{port}/#/".format(protocal=http_protocal, host=host,
                                                     port=port)
        if self.driver is None:
            driver = self._init_driver()
        else:
            driver = self.driver

        if port == "443":
            url = url.replace(":%s/" % port, "")
        if url != driver.current_url:
            driver.get(url)

    def _get_acmp_item_value(self, item=None):
        tdata = self.tdata
        for resource in tdata["SALMON_RESOURCE"].values():
            if not isinstance(resource, list):
                resource = [resource]

            for res in resource:
                if res["category"] == "ACMP":
                    return res["descriptor"][item]

    def _wait_until(self, predicate, timeout=60, poll_frequency=1, message="",
                    ignored_exceptions=None):
        if message == '':
            message = "After %s seconds, the func %s also return False" % \
                      (timeout, predicate.__name__)
        screen = None
        stacktrace = None
        end_time = time.time() + timeout
        while True:
            try:
                value = predicate()
                if value:
                    return value
            except ignored_exceptions as exc:
                screen = getattr(exc, 'screen', None)
                stacktrace = getattr(exc, 'stacktrace', None)
            time.sleep(poll_frequency)
            if time.time() > end_time:
                break
        raise TimeoutException(message, screen, stacktrace)
		
	 def _get_acmp_ip(self):
        return self.env_info.acmp_ip

    def _get_acmp_port(self):
        return self.env_info.acmp_port

    def _get_acmp_url(self):
        return "%s:%s" % (self._get_acmp_ip(), self._get_acmp_port())

    def _get_acmp_username(self):
        return self.env_info.acmp_username

    def _get_acmp_password(self):
        return self.env_info.acmp_password
		
	    def _get_cluster_item_value(self, item=None):
        # TODO to get the cluster info from EnvInfo
        tdata = self.tdata
        for resource in tdata["SALMON_RESOURCE"].values():
            if not isinstance(resource, list):
                resource = [resource]

            for res in resource:
                if res["category"] == "CLUSTER":
                    return res["descriptor"][item]

    def _get_cluster_ip(self):
        # TODO to get the cluster ip from EnvInfo
        return self._get_cluster_item_value(item="cluster_ip")

    def _get_cluster_password(self):
        # TODO to get the cluster password from EnvInfo
        return self._get_cluster_item_value(item="password")

    def _get_clusters_info(self):
        tdata = self.tdata
        info_list = []
        for resource in tdata["SALMON_RESOURCE"].values():
            if not isinstance(resource, list):
                resource = [resource]

            for res in resource:
                if res["category"] == "CLUSTER":
                    info = {
                        "host": res["descriptor"]["cluster_ip"],
                        "user": res["descriptor"]["user_name"],
                        "password": res["descriptor"]["password"]
                    }
                    info_list.append(info)

        if not info_list:
            raise RuntimeError("Failed to find any cluster in %s" % tdata)
        return info_list

    def _get_zone_by_cluster(self, cluster_ip):
        # TODO to get the cluster password from EnvInfo
        tdata = self.tdata
        cluster_id = None
        for resource in tdata["SALMON_RESOURCE"].values():
            if not isinstance(resource, list):
                resource = [resource]

            for res in resource:
                if res["category"] == "CLUSTER" and res["descriptor"]["cluster_ip"] == cluster_ip:
                    cluster_id = res["_id"]
                    break
        if not cluster_id:
            raise ValueError("Failed to find cluster_ip(%s) in %s" % (cluster_ip, tdata))

        for resource in tdata["SALMON_RESOURCE"].values():
            if not isinstance(resource, list):
                resource = [resource]

            for res in resource:
                if res["category"] == "ZONE" and cluster_id in res["descriptor"]["clusters"]:
                    return res["descriptor"]["name"]
            raise ValueError("Failed to find cluster_id(%s) in %s" % (cluster_id, tdata))

    @classmethod
    def get_acmp_info(cls, item):
        tdata = cls.tdata
        for resource in tdata["SALMON_RESOURCE"].values():
            if not isinstance(resource, list):
                resource = [resource]

            for res in resource:
                if res["category"] == "ACMP":
                    return res["descriptor"][item]
	
	    def _init_admin_pages_clients(self, host=None, port=None):
        LOG.info("to init admin pages clients")
        conf = self._init_conf()
        begin = time.time()
        if self.driver is None:
            driver = self._init_driver()
        else:
            driver = self.driver
        LOG.debug("Initialing driver spends %s seconds" % (time.time() - begin))

        # init all clients
        self._init_clients(driver=driver, conf=conf)
        self.monkey_patch_element_click()

        if host is None:
            host = self._get_acmp_ip()

        if port is None:
            port = self._get_acmp_port()

        http_protocal = "https"  # or "http"
        # admin_web_port = 4430
        admin_url = "{protocal}://{host}:{port}/login".format(protocal=http_protocal,
                                                              host=host, port=port)

        LOG.info("To log in: %s" % admin_url)
        begin = time.time()

        driver.get(admin_url)
        LOG.debug("Driver visiting url spends %s seconds" % (time.time() - begin))

    def _init_admin_api_clients(self):
        acmp_conf = self.get_acmp_conf()
        # acloud_conf_api = self.get_acloud_conf()

        self._init_api_clients(acmp_conf=acmp_conf)

    @classmethod
    def _init_api_clients(cls, acmp_conf=None):
        # cloud_host
        cls.cloud_host_api_client = cloud_host_api.Client(**acmp_conf)
        # organization operate
        cls.org_api_client = org_api.Client(**acmp_conf)
        # role operate
        cls.role_api_client = role_api.Client(**acmp_conf)
        # eip pool
        cls.eip_pool_api_client = eip_pool_api.Client(**acmp_conf)
        # topo
        cls.innerdns_api_client = domain_api.Client(**acmp_conf)

        cls.privateline_api_client = privateline_api.Client(**acmp_conf)
		
	 @classmethod
    def _init_clients(cls, driver=None, conf=None):
        # init cluster client
        cls.cluster_client = cluster.Client(driver=driver, conf=conf)

        # init guide
        cls.guide_client = guide.Client(driver=driver, conf=conf)
		####
	
	    def monkey_patch_element_click(self):
        original_method = self.ELEMENT_CLICK_METHOD
        notify_bubble_ok_client = self.notify_bubble_ok_client

        def new_click(self):
            try:
                original_method(self)
            except WebDriverException as e:
                if "Other element would receive the click" in unicode(e):
                    LOG.warning(
                        u"Other element would receive the click, now try again: %s" % unicode(e))

                    if re.search(
                            r"Other element would receive the click.*notify-bubble", unicode(e)):
                        notify_bubble_ok_client.wait_notify_disappear()
                        original_method(self)
                    else:
                        original_method(self)
                else:
                    raise

        WebElement.click = new_click

    def step_open_admin_page(self, host=None, port=None):
        LOG.step("to open admin page in browser")
        self._init_admin_api_clients()
        self._init_admin_pages_clients(host=host, port=port)

    @allure.step(u"登录admin")
    def step_login(self, username=None, password=None, retry=False):
        LOG.step("to login as admin")

        env_info = env.EnvInfo()
        env_info.login_type = "admin"

        if username is None:
            username = self._get_acmp_username()
        else:
            env_info.acmp_username = username

        if password is None:
            password = self._get_acmp_password()
        else:
            env_info.acmp_password = password

        retry_times = 3 if retry else 1

        def wait(x):
            try:
                self.admin_login_client.wait_page_loaded(timeout=10)
                return True
            except TimeoutException:
                self.admin_login_client.refresh_page()
                return False

        def login():
            # TODO: When salmon can save network requests, don't retry any more
            for i in range(retry_times):
                try:
                    self.admin_login_client._wait_until(wait, timeout=60)
                    self.admin_login_client.login(username, password)
                    self.admin_login_client.wait_login_success()
                    # self.navigation_clint.switch_to_tab_management()
                    # self.admin_login_client.wait_login_success()
                    return
                except TimeoutException:
                    if i < retry_times - 1:
                        LOG.warning("Failed to login, now try again(%s)" % i)
                        self.step_open_admin_page()
                    else:
                        raise

        login()
	
	    def step_wait_login_success(self, **kwargs):
        LOG.step("to wait login success")
        self.admin_login_client.wait_login_success()

    @allure.step(u"登录manager")
    def step_manager_login(self, selector=None, expect_successfully=True):
        LOG.step("to login manager")
        config = fresh_parse_data(selector=selector, tdata=self.tdata).values()[0]
        manager_name = config.get("name")
        manager_password = config.get("password")

        def wait(x):
            try:
                self.admin_login_client.wait_page_loaded(timeout=10)
                return True
            except TimeoutException:
                self.admin_login_client.refresh_page()
                return False

        self.admin_login_client._wait_until(wait, timeout=60)
        self.admin_login_client.login(username=manager_name, password=manager_password)
        if expect_successfully:
            self.admin_login_client.wait_login_success()
            return True
        else:
            self.admin_login_client.wait_login_get_fail_info(timeout=10)
            login_fail_info = self.admin_login_client.login_fail_info
            return login_fail_info

    def check_manager_login_fail_info(self, selector=None, expect_error=None,
                                      expect_successfully=False):
        error_info = self.step_manager_login(selector=selector,
                                             expect_successfully=expect_successfully)
        if expect_error in error_info:
            return True
        raise ValueError(u"与报错提示信息不匹配！")