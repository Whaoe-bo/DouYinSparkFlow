import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from utils import norm
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
from playwright.sync_api import Response
import time
 
config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
userIDDict = {}
 
CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"
 
 
def handle_response(response: Response):
    """
    只监听 im/user/info 接口的响应，收集好友信息用于匹配
 
    [修复] 该接口有时返回 {"data": null}（甚至整个 body 为 null）。
    dict.get(key, default) 只在 key 不存在时返回 default，
    key 存在但值为 None 时仍返回 None，导致 for 循环抛
    'NoneType' object is not iterable。改用 `or []` 兜底。
    """
    global userIDDict
 
    if "aweme/v1/web/im/user/info" not in response.url:
        return
 
    try:
        json_data = response.json()
 
        # body 本身可能就是 null / 非对象
        if not isinstance(json_data, dict):
            return
 
        # 关键修复：用 or [] 而不是 get 的默认值
        data = json_data.get("data") or []
        if not isinstance(data, list):
            return
 
        for item in data:
            if not isinstance(item, dict):
                continue
            short_id = item.get("short_id")  # short_id
            unique_id = item.get("unique_id")  # unique_id
            sec_uid = item.get("sec_uid", "")  # sec_uid 可能不存在，提供默认值为空字符串
            nickname = norm(item.get("nickname"))  # 昵称
            remark_name = norm(item.get("remark_name", nickname))  # 备注名，如果没有则使用昵称
            userIDDict[remark_name] = [short_id, unique_id, sec_uid, nickname, remark_name]
    except Exception:
        # 非 JSON / 连接中断等异常，降级为 debug，不再刷屏
        logger.debug("解析 im/user/info 响应失败: %s", response.url, exc_info=True)
 
 
def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    :param name: 操作名称（用于日志记录）
    :param operation: 要执行的异步操作
    :param retries: 最大重试次数
    :param delay: 每次重试之间的延迟（秒）
    :param args: 传递给操作的参数
    :param kwargs: 传递给操作的关键字参数
    """
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise
 
 
def checkTargetName(targetName, targets):
    """检查targetName是否为目标
    """
    targetSymbol = None
 
    targetName = norm(targetName)
 
    if targetName in userIDDict:
        matched = next((v for v in userIDDict[targetName] if v and v in targets), None)
        if matched is not None:
            targetSymbol = matched
    else:
        if targetName in targets:
            targetSymbol = targetName
    return targetSymbol
 
 
def scroll_and_select_user(page, username, targets):
    """尝试滚动并查找用户名"""
    # 定义目标元素和滚动容器的选择器
    target_selector = CONVERSATION_ITEM_SELECTOR
    scrollable_friends_selector = CONVERSATION_LIST_SELECTOR
 
    # [修复] 使用模糊匹配 no-more-tip- 前缀，不再依赖精确哈希后缀
    # 同时增加文本匹配作为兜底
    # no_more_selector = 'xpath=//div[contains(@class, "no-more-tip-")]'
    # loading_selector = 'xpath=//div[contains(@class, "semi-spin")]'
 
    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")
 
    found_targets = set()
    # [修改] 复制一份目标列表用于追踪进度
    remaining_targets = set(targets)
 
    # [修复] 新增：连续空滚动计数器（滚动后没有发现新好友的次数）
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10  # 连续10次滚动没有新好友，认为到底了
 
    while True:
        # 查找所有目标元素
        target_elements = page.locator(target_selector).all()
        # [修复] 记录本轮循环前已发现的好友数，用于判断是否有新发现
        prev_found_count = len(found_targets)
 
        for element in target_elements:
            try:
                # 查找子元素 span，模糊匹配 class
                span = element.locator(CONVERSATION_TITLE_SELECTOR)
                targetName = span.inner_text()
                if targetName in found_targets:
                    continue  # 已处理过，跳过
                found_targets.add(targetName)
                logger.debug(f"账号 {username} 找到好友 {targetName}")
 
                targetSymbol = checkTargetName(targetName, targets)
                if targetSymbol:
                    element.click()
 
                    yield targetSymbol
 
                    # [修改] 标记已找到，如果全找到了直接退出
                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                    break
            except Exception as e:
                traceback.print_exc()
        else:
            # [修复] 检查本轮是否有新好友被发现
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0  # 有新发现，重置计数器
            else:
                empty_scroll_count += 1  # 无新发现，递增计数器
 
            # [修复] 状态检测逻辑（多重兜底）
            # # 1. 检查是否到底（"没有更多了" —— 使用模糊类名匹配）
            # if page.locator(no_more_selector).count() > 0:
            #     logger.info(f"账号 {username} 检测到'没有更多了'标志，已到达底部")
            #     if len(remaining_targets) > 0:
            #         logger.warning(
            #             f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
            #         )
            #     break
 
            # 2. [修复] 检查连续空滚动次数，防止死循环
            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(
                    f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部"
                )
                if len(remaining_targets) > 0:
                    logger.warning(
                        f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}"
                    )
                break
 
            # 3. 检查是否正在加载
            # if page.locator(loading_selector).count() > 0:
            #     logger.debug(f"账号 {username} 列表正在加载中 (Loading)...")
            #     time.sleep(1.5)  # 给加载留点时间
            #     # 不 break，继续去滚动以触发后续内容
 
            # 4. 滚动容器
            scrollable_element = page.locator(
                scrollable_friends_selector
            ).element_handle()
            if scrollable_element:
                # [修复] 记录滚动前的 scrollTop，用于检测是否真的滚动了
                scroll_top_before = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )
                page.evaluate(
                    "(element) => element.scrollTop += 800", scrollable_element
                )
                # [修复] 检测滚动后的 scrollTop
                time.sleep(0.3)
                scroll_top_after = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )
                if scroll_top_before == scroll_top_after:
                    # scrollTop 没有变化，说明已经到底了
                    empty_scroll_count += 2  # 加速判定到底
                    logger.debug(
                        f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 (空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
                    )
                else:
                    logger.debug(
                        f"账号 {username} 滚动好友列表以加载更多好友 (scrollTop: {scroll_top_before} -> {scroll_top_after})"
                    )
                time.sleep(1.5)
            else:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break
 
 
def do_user_task(browser, username, cookies, targets):
    context = browser.new_context()  # 每个任务使用独立的上下文
    context.set_default_navigation_timeout(
        config["browserTimeout"]
    )  # 设置导航超时时间为 120 秒
    context.set_default_timeout(
        config["browserTimeout"]
    )  # 设置所有操作的默认超时时间为 120 秒
    page = context.new_page()
    page.on("response", handle_response)  # 监听响应，收集好友完整信息用于匹配
 
    # 注入 Cookie
    context.add_cookies(cookies)
 
    # 打开抖音网页聊天页面
    # [修复] 抖音聊天页是长连接持续加载的页面，wait_until="load" 可能永远不会触发，
    # 导致每次都要卡满 120 秒导航超时。改为只等 DOM 就绪。
    retry_operation(
        "打开抖音网页聊天页面",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://www.douyin.com/chat",
        wait_until="domcontentloaded",
    )
 
    # [修复] 用显式等待目标元素代替盲目 sleep，比等 load 事件可靠
    try:
        page.wait_for_selector(
            CONVERSATION_LIST_SELECTOR, timeout=config["browserTimeout"]
        )
        logger.debug(f"账号 {username} 聊天列表已加载")
    except Exception as e:
        logger.warning(f"账号 {username} 等待聊天列表容器超时：{e}")
 
    time.sleep(3)  # 留一点时间让可能存在的弹窗消失
 
    logger.debug(f"账号 {username} 开始发送消息")
 
    # 滚动并选择用户
    # [修复] 原来这里用 username 接收 yield 值，会覆盖账号名，
    # 导致日志里"账号 刘晗 给好友 刘晗 发送消息"这种账号名丢失的记录。
    for target in scroll_and_select_user(page, username, targets):
        logger.debug(f"账号 {username} 已选中好友 {target} 发送消息")
 
        # 等待聊天输入框元素加载完成，使用更稳定的属性选择器
        chat_input_selector = CHAT_EDITOR_SELECTOR
        page.wait_for_selector(chat_input_selector, timeout=config["browserTimeout"])
        chat_input = page.locator(chat_input_selector)
 
        # 在 chat-input-dccKiL 中输入内容
        message = build_message()
        lines = message.split("\\n")
        for index, line in enumerate(lines):
            chat_input.type(line)  # 输入每一行
            # 如果不是最后一行，模拟 Shift+Enter 插入换行
            if index != len(lines) - 1:
                chat_input.press("Shift+Enter")  # 模拟 Shift+Enter 插入换行
 
        logger.debug(f"账号 {username} 准备发送消息给好友 {target}：\n\t{message}")
        logger.debug(f"账号 {username} 给好友 {target} 发送消息完成")
 
        # 模拟按下回车键发送消息
        chat_input.press("Enter")
        time.sleep(2)  # 发送完等待一会儿
 
    context.close()  # 任务完成后关闭上下文
 
 
def runTasks():
    playwright, browser = get_browser()
    try:
        # 检查是否启用多任务和任务数量
        # 创建信号量以限制并发任务数量
        logger.info("开始执行任务")
        logger.debug(f"当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
 
        for user in userData:
            logger.debug(
                f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}"
            )
 
        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
 
            # 创建任务
            do_user_task(browser, username, cookies, targets)
            logger.info(f"账号 {username} 任务完成")
    finally:
        # 关闭浏览器实例
        browser.close()
        playwright.stop()
