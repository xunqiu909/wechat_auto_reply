"""
Author: Cluic / Adapted for WeChat 4.x
Version: 4.1.8.27
WeChat 4.1 UIA-based automation (Qt5/mmui framework)
"""
from . import uiautomation as uia
from .languages import *
from .utils import *
from .elements import *
from .errors import *
from .color import *
import time
import os
import re
try:
    from typing import Literal
except:
    from typing_extensions import Literal


class WeChat(WeChatBase):
    """微信4.1 UI自动化实例 (Qt5/mmui框架)

    UIA控件树结构:
        mmui::MainWindow(微信)
        └── QWidget
            ├── QStackedWidget
            │   └── mmui::MainView
            │       ├── mmui::MainTabBar(导航)
            │       │   ├── XTabBarItem(微信/通讯录/收藏/朋友圈...)
            │       │   └── ...
            │       └── QWidget
            │           └── mmui::MainView
            │               └── XSplitterView → XStackedWidget → XSplitterView
            │                   ├── XStackedWidget(RIGHT=聊天详情)
            │                   │   └── ChatDetailView
            │                   │       ├── ChatMessagePage(有聊天时)
            │                   │       │   ├── ChatTitleBarMasterView
            │                   │       │   ├── XSplitterView
            │                   │       │   │   ├── InputView→ChatInputField
            │                   │       │   │   ├── MessageView→RecyclerListView
            │                   │       │   │   └── QSplitterHandle
            │                   │       │   └── ..
            │                   │       └── ChatPage(空状态, 无聊天时)
            │                   ├── XView(LEFT=会话列表)
            │                   │   └── ChatMasterView
            │                   │       └── XView
            │                   │           ├── XView(搜索栏区)
            │                   │           └── XView(列表区)
            │                   │               └── ChatSessionList→XTableView
            │                   └── QSplitterHandle
            └── mmui::TitleBar
    """
    VERSION: str = '4.1.8.27'
    lastmsgid: str = None
    listen: dict = dict()
    SessionItemList: list = []

    def __init__(
            self,
            language: Literal['cn', 'cn_t', 'en'] = 'cn',
            debug: bool = False
        ) -> None:
        """微信UI自动化实例

        Args:
            language: 微信客户端语言版本, 可选: cn简体中文 cn_t繁体中文 en英文
            debug: 是否开启调试日志
        """
        set_debug(debug)
        self.language = language
        self._show()
        self._init_navigation()
        self.nickname = self._get_nickname()
        self.usedmsgid = []
        print(f'初始化成功，获取到已登录窗口：{self.nickname}')

    # ========== 内部方法 ==========

    def _show(self):
        """激活微信窗口"""
        # UIA SwitchToThisWindow 通常比 SetForegroundWindow 可靠
        try:
            self.UiaAPI = uia.WindowControl(ClassName='mmui::MainWindow', searchDepth=1)
            self.UiaAPI.SwitchToThisWindow()
        except:
            pass

    def _get_nickname(self):
        """获取当前登录用户昵称"""
        try:
            # 从导航栏的第一个XTabBarItem获取(有个XSkiaWidget可能包含头像相关信息)
            nav_bar = self.NavigationBox
            # 通过查找我的头像位置获取昵称 - 在标题栏附近的用户区
            # 从窗口标题获取不了, 从会话列表页获取个人信息
            return self.UiaAPI.Name  # '微信'
        except:
            return '微信用户'

    def _init_navigation(self):
        """初始化UIA控件树导航引用"""
        uia.SetGlobalSearchTimeout(3)

        rootWidget = self.UiaAPI.GroupControl(searchDepth=1)
        stacked = rootWidget.CustomControl(ClassName='QStackedWidget')
        mainView = stacked.GroupControl(ClassName='mmui::MainView')

        # 导航栏
        self.NavigationBox = mainView.ToolBarControl(ClassName='mmui::MainTabBar')

        # 导航按钮
        self.A_ChatIcon = self.NavigationBox.ButtonControl(Name='微信')
        self.A_ContactsIcon = self.NavigationBox.ButtonControl(Name='通讯录')
        self.A_FavoritesIcon = self.NavigationBox.ButtonControl(Name='收藏')
        self.A_MomentsIcon = self.NavigationBox.ButtonControl(Name='朋友圈')
        self.A_MiniProgram = self.NavigationBox.ButtonControl(Name='小程序面板')

        # 内容区域
        contentWidget = [c for c in mainView.GetChildren()
                         if c.ControlTypeName == 'GroupControl' and c.ClassName == 'QWidget'][0]
        innerMainView = contentWidget.GroupControl(ClassName='mmui::MainView')
        splitter = innerMainView.CustomControl(ClassName='mmui::XSplitterView')
        outerStack = splitter.CustomControl(ClassName='mmui::XStackedWidget')
        self.outerSplitter = outerStack.CustomControl(ClassName='mmui::XSplitterView')

        # 左侧 — 会话列表, 右侧 — 聊天详情
        children = self.outerSplitter.GetChildren()
        xviews = [c for c in children if c.ClassName == 'mmui::XView']
        stacked_views = [c for c in children if c.ClassName == 'mmui::XStackedWidget']
        self.leftPanel = xviews[0] if xviews else None
        self.rightPanel = stacked_views[0] if stacked_views else None

        # 搜索框
        chatMasterView = self.leftPanel.GroupControl(ClassName='mmui::ChatMasterView')
        chatListInner = chatMasterView.GroupControl(ClassName='mmui::XView')
        searchField = chatListInner.GroupControl(ClassName='mmui::XSearchField')
        self.B_Search = searchField.EditControl(ClassName='mmui::XValidatorTextEdit')

        uia.SetGlobalSearchTimeout(10)

    def _fresh_window(self):
        """强制获取全新的 UIA 窗口引用 (通过Win32句柄避免缓存)"""
        uia.SetGlobalSearchTimeout(5)
        hwnd = win32gui.FindWindow(None, '微信')
        if not hwnd:
            return uia.WindowControl(ClassName='mmui::MainWindow', searchDepth=1)
        try:
            return uia.ControlFromHandle(hwnd)
        except:
            return uia.WindowControl(ClassName='mmui::MainWindow', searchDepth=1)

    def _nav_right_panel(self):
        """从根窗口重新导航到右侧面板"""
        try:
            win = self._fresh_window()
            stacked = rootWidget.CustomControl(ClassName='QStackedWidget')
            mainView = stacked.GroupControl(ClassName='mmui::MainView')
            contentWidget = [c for c in mainView.GetChildren()
                           if c.ControlTypeName == 'GroupControl' and c.ClassName == 'QWidget'][0]
            innerMainView = contentWidget.GroupControl(ClassName='mmui::MainView')
            splitter = innerMainView.CustomControl(ClassName='mmui::XSplitterView')
            outerStack = splitter.CustomControl(ClassName='mmui::XStackedWidget')
            outerSplitter = outerStack.CustomControl(ClassName='mmui::XSplitterView')
            children = outerSplitter.GetChildren()
            rightPanel = [c for c in children if c.ClassName == 'mmui::XStackedWidget'][0]
            uia.SetGlobalSearchTimeout(10)
            return rightPanel
        except Exception:
            uia.SetGlobalSearchTimeout(10)
            return None

    def _nav_left_panel(self):
        """从根窗口重新导航到左侧面板"""
        try:
            win = self._fresh_window()
            rootWidget = win.GroupControl(searchDepth=1)
            stacked = rootWidget.CustomControl(ClassName='QStackedWidget')
            mainView = stacked.GroupControl(ClassName='mmui::MainView')
            contentWidget = [c for c in mainView.GetChildren()
                           if c.ControlTypeName == 'GroupControl' and c.ClassName == 'QWidget'][0]
            innerMainView = contentWidget.GroupControl(ClassName='mmui::MainView')
            splitter = innerMainView.CustomControl(ClassName='mmui::XSplitterView')
            outerStack = splitter.CustomControl(ClassName='mmui::XStackedWidget')
            outerSplitter = outerStack.CustomControl(ClassName='mmui::XSplitterView')
            children = outerSplitter.GetChildren()
            leftPanel = [c for c in children if c.ClassName == 'mmui::XView'][0]
            uia.SetGlobalSearchTimeout(10)
            return leftPanel
        except Exception:
            uia.SetGlobalSearchTimeout(10)
            return None

    def _get_session_table(self):
        """获取会话列表控件(每次重新导航)"""
        leftPanel = self._nav_left_panel()
        if leftPanel is None:
            return None
        try:
            chatMasterView = leftPanel.GroupControl(ClassName='mmui::ChatMasterView')
            chatListInner = chatMasterView.GroupControl(ClassName='mmui::XView')
            listAreas = [c for c in chatListInner.GetChildren() if c.ClassName == 'mmui::XView']
            if len(listAreas) < 2:
                return None
            listArea = listAreas[1]
            listInner = listArea.GroupControl(ClassName='mmui::XView')
            sessionList = listInner.GroupControl(ClassName='mmui::ChatSessionList')
            return sessionList.ListControl(ClassName='mmui::XTableView')
        except:
            return None

    def _get_search_edit(self):
        """获取搜索框(每次重新导航)"""
        leftPanel = self._nav_left_panel()
        if leftPanel is None:
            return None
        try:
            chatMasterView = leftPanel.GroupControl(ClassName='mmui::ChatMasterView')
            chatListInner = chatMasterView.GroupControl(ClassName='mmui::XView')
            searchField = chatListInner.GroupControl(ClassName='mmui::XSearchField')
            return searchField.EditControl(ClassName='mmui::XValidatorTextEdit')
        except:
            return None

    def _get_chat_message_page(self):
        """获取聊天消息页面"""
        rightPanel = self._nav_right_panel()
        if rightPanel is None:
            return None
        try:
            cdd = rightPanel.GroupControl(ClassName='mmui::ChatDetailView')
            # ChatMessagePage 当聊天打开时, ChatPage 当空状态时
            try:
                return cdd.GetFirstChildControl()
            except:
                pass
        except:
            pass
        return None

    def _get_msg_list(self):
        """获取消息列表控件"""
        chatPage = self._get_chat_message_page()
        if chatPage is None:
            return None
        try:
            splitter = chatPage.CustomControl(ClassName='mmui::XSplitterView')
            messageView = [c for c in splitter.GetChildren()
                          if c.ClassName == 'mmui::MessageView'][0]
            return messageView.ListControl(ClassName='mmui::RecyclerListView')
        except:
            return None

    def _get_input_field(self):
        """获取聊天输入框"""
        chatPage = self._get_chat_message_page()
        if chatPage is None:
            return None
        try:
            splitter = chatPage.CustomControl(ClassName='mmui::XSplitterView')
            inputView = [c for c in splitter.GetChildren()
                        if c.ClassName == 'mmui::XView'][0]
            inputGroup = inputView.GroupControl(ClassName='mmui::InputView')
            xview = inputGroup.GroupControl(ClassName='mmui::XView')
            innerXview = xview.GroupControl(ClassName='mmui::XView')
            return innerXview.EditControl(ClassName='mmui::ChatInputField')
        except:
            return None

    def _parse_session_cell(self, item):
        """解析会话列表项, 返回 (name, unread_count, last_msg, time_str)"""
        name_text = item.Name
        lines = name_text.split('\n')

        name = lines[0] if lines else ''
        unread = 0
        last_msg = ''
        time_str = ''

        # 解析未读数
        for line in lines:
            m = re.search(r'\[(\d+)条\]', line)
            if m:
                unread = int(m.group(1))
                break

        # 解析时间: 最后一行通常是时间或状态
        if lines:
            last_line = lines[-1]
            time_match = re.match(r'^\d{1,2}:\d{2}$', last_line)
            msg_free_match = re.match(r'^消息免打扰$', last_line)
            if time_match or msg_free_match:
                if msg_free_match and len(lines) >= 2:
                    time_str = lines[-2]
                else:
                    time_str = last_line

        # 解析最后消息: 在名字和最后一个状态行之间
        # 格式: name\n[XX条] \n发送者: 内容\n时间
        content_lines = []
        for line in lines[1:]:
            if re.match(r'^\d{1,2}:\d{2}$', line):
                break
            if line == '消息免打扰':
                break
            if '[条]' in line:
                continue
            content_lines.append(line)

        last_msg = ' '.join(content_lines).strip()

        return name, unread, last_msg, time_str

    def _parse_message_items(self, items):
        """解析消息列表项，返回Message对象列表"""
        msgs = []
        for item in items:
            cls = item.ClassName
            name = item.Name
            rid = ''.join([str(i) for i in item.GetRuntimeId()])

            if cls == 'mmui::ChatItemView':
                msgs.append(TimeMessage(['Time', name, rid], item, self))
            elif cls == 'mmui::ChatTextItemView':
                # Name可能是 "sender_id\ncontent"(群聊) 或纯 "content"(单聊)
                if '\n' in name and len(name.split('\n')[0]) < 30:
                    parts = name.split('\n', 1)
                    sender = parts[0]
                    content = parts[1] if len(parts) > 1 else name
                    msgs.append(FriendMessage([(sender, sender), content, rid], item, self))
                else:
                    # 单聊模式 - 无法区分谁发的, 标记为 'text' 类型
                    # 对监控来说, 所有消息都应该检查
                    msgs.append(FriendMessage([('unknown', 'unknown'), name, rid], item, self))
            elif 'File' in cls or 'Image' in cls or 'Video' in cls:
                msgs.append(SysMessage(['SYS', '[{}]'.format(name), rid], item, self))
            else:
                if name:
                    msgs.append(SysMessage(['SYS', name, rid], item, self))
        return msgs

    # ========== 公共API ==========

    def ChatWith(self, who, timeout=5):
        """打开某个聊天会话

        Args:
            who: 要打开的聊天对象名(完整匹配)
            timeout: 超时时间(秒)
        Returns:
            chatname: 成功返回名字, 失败返回False
        """
        self._show()
        uia.SetGlobalSearchTimeout(3)

        def _click_item(item):
            """坐标点击"""
            r = item.BoundingRectangle
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            win32api.SetCursorPos((cx, cy))
            time.sleep(0.03)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

        # 直接在当前可见列表或向下扫描中找
        table = self._get_session_table()
        if table:
            # 先查可见列表
            for item in table.GetChildren():
                name = item.Name.split('\n')[0]
                if who == name:
                    _click_item(item)
                    time.sleep(0.5)
                    uia.SetGlobalSearchTimeout(10)
                    return name

            # 快速向下滚动扫描(最多滚10页)
            seen = set()
            for _ in range(10):
                table.WheelDown(wheelTimes=15, waitTime=0.01, interval=0.01)
                time.sleep(0.3)
                table = self._get_session_table()
                if table is None:
                    break
                items = table.GetChildren()
                cur_names = set()
                for item in items:
                    n = item.Name.split('\n')[0]
                    cur_names.add(n)
                    if who == n:
                        _click_item(item)
                        time.sleep(0.5)
                        uia.SetGlobalSearchTimeout(10)
                        return n
                if cur_names.issubset(seen):
                    break
                seen.update(cur_names)

            # 再向上扫描(如果目标在上方)
            for _ in range(10):
                table.WheelUp(wheelTimes=15, waitTime=0.01, interval=0.01)
                time.sleep(0.3)
                table = self._get_session_table()
                if table is None:
                    break
                items = table.GetChildren()
                cur_names = set()
                for item in items:
                    n = item.Name.split('\n')[0]
                    cur_names.add(n)
                    if who == n:
                        _click_item(item)
                        time.sleep(0.5)
                        uia.SetGlobalSearchTimeout(10)
                        return n
                if cur_names.issubset(seen):
                    break
                seen.update(cur_names)

        uia.SetGlobalSearchTimeout(10)
        return False

    def SendMsg(self, msg, who=None, clear=True, at=None):
        """发送文本消息

        Args:
            msg: 要发送的文本消息
            who: 要发送给谁, None则发送到当前聊天窗口
            clear: 是否清除原有内容
            at: 要@的人(str或list)
        """
        if not msg and not at:
            return None

        if who:
            self.ChatWith(who)
            time.sleep(0.3)

        self._show()

        input_field = self._get_input_field()
        if input_field is None:
            wxlog.debug('未找到输入框, 尝试坐标点击')
            # 获取ChatMessagePage坐标
            chatPage = self._get_chat_message_page()
            if chatPage:
                r = chatPage.BoundingRectangle
                # 输入框在底部
                cx = (r.left + r.right) // 2
                cy = r.bottom - 80
                Click((uia.RECT(r.left, cy, r.right, cy + 40)))
                time.sleep(0.2)

        # 处理@
        if at:
            if isinstance(at, str):
                at = [at]
            for person in at:
                if input_field:
                    input_field.SendKeys('@' + person)
                    time.sleep(0.2)
                    input_field.SendKeys('{Enter}')
                    time.sleep(0.1)
                if msg and not msg.startswith('\n'):
                    msg = '\n' + msg

        if msg:
            if clear and input_field:
                input_field.SendKeys('{Ctrl}a', waitTime=0)
                time.sleep(0.05)

            t0 = time.time()
            while True:
                if time.time() - t0 > 10:
                    raise TimeoutError(f'发送消息超时 --> {msg}')
                SetClipboardText(msg)
                if input_field:
                    input_field.SendKeys('{Ctrl}v')
                else:
                    self.UiaAPI.SendKeys('{Ctrl}v')
                time.sleep(0.2)
                # 检查是否粘贴成功
                if get_clipboard_text() == msg:
                    break

        if input_field:
            input_field.SendKeys('{Enter}')
        else:
            self.UiaAPI.SendKeys('{Enter}')

    def SendFiles(self, filepath, who=None):
        """发送文件

        Args:
            filepath: 文件绝对路径(str|list)
            who: 接收方

        Returns:
            bool: 是否成功
        """
        if who:
            self.ChatWith(who)
            time.sleep(0.3)

        filelist = []
        if isinstance(filepath, str):
            if not os.path.exists(filepath):
                Warnings.lightred(f'未找到文件：{filepath}', stacklevel=2)
                return False
            filelist.append(os.path.realpath(filepath))
        elif isinstance(filepath, (list, tuple, set)):
            for f in filepath:
                if os.path.exists(f):
                    filelist.append(f)
                else:
                    Warnings.lightred(f'未找到文件：{f}', stacklevel=2)
        else:
            Warnings.lightred(f'filepath参数格式错误：{type(filepath)}', stacklevel=2)
            return False

        if not filelist:
            return False

        self._show()
        input_field = self._get_input_field()
        t0 = time.time()
        while True:
            if time.time() - t0 > 10:
                raise TimeoutError(f'发送文件超时 --> {filelist}')
            SetClipboardFiles(filelist)
            time.sleep(0.2)
            if input_field:
                input_field.SendKeys('{Ctrl}v')
            else:
                self.UiaAPI.SendKeys('{Ctrl}v')
            time.sleep(0.3)
            break
        time.sleep(0.2)
        if input_field:
            input_field.SendKeys('{Enter}')
        else:
            self.UiaAPI.SendKeys('{Enter}')
        return True

    def GetSessionList(self, reset=False, newmessage=False):
        """获取聊天列表

        Args:
            reset: 是否重置SessionItemList
            newmessage: 是否只返回有新消息的

        Returns:
            dict: {会话名: 未读消息数}
        """
        self._show()
        if reset:
            self.SessionItemList = []

        table = self._get_session_table()
        if table is None:
            return {}

        items = table.GetChildren()
        sessionList = {}

        for item in items:
            name, unread, _, _ = self._parse_session_cell(item)
            if name:
                if name not in self.SessionItemList:
                    self.SessionItemList.append(name)
                sessionList[name] = unread

        if newmessage:
            return {k: v for k, v in sessionList.items() if v > 0}
        return sessionList

    def GetSession(self):
        """获取当前聊天列表中的所有聊天对象

        Returns:
            list: SessionElement对象列表
        """
        self._show()
        table = self._get_session_table()
        if table is None:
            return []
        return [SessionElement(item) for item in table.GetChildren()]

    def GetAllMessage(self, savepic=False, savefile=False, savevoice=False):
        """获取当前聊天窗口中加载的所有聊天记录

        Returns:
            list: 消息列表
        """
        msgList = self._get_msg_list()
        if msgList is None:
            return []

        items = msgList.GetChildren()
        msgs = self._parse_message_items(items)
        return msgs

    def LoadMoreMessage(self):
        """加载更多聊天记录"""
        msgList = self._get_msg_list()
        if msgList is None:
            return False

        loadmore = msgList.GetFirstChildControl()
        if loadmore is None:
            return False

        loadmore_top = loadmore.BoundingRectangle.top
        top = msgList.BoundingRectangle.top

        for _ in range(50):  # 最多滚50次
            if loadmore.BoundingRectangle.top > top or loadmore.Name == '':
                return True
            msgList.WheelUp(wheelTimes=10, waitTime=0.1)
            if loadmore.BoundingRectangle.top == loadmore_top:
                return False
            loadmore_top = loadmore.BoundingRectangle.top

        msgList.WheelUp(wheelTimes=1, waitTime=0.1)
        return True

    def CurrentChat(self):
        """获取当前聊天对象名 - 从标题栏 XTextView 获取"""
        try:
            rightPanel = self._nav_right_panel()
            if rightPanel is None:
                return None
            # 直接从 ChatDetailView 获取第一个子控件 (ChatMessagePage 或 ChatPage)
            cdd = rightPanel.GroupControl(ClassName='mmui::ChatDetailView')
            try:
                chatPage = cdd.GetFirstChildControl()
            except:
                return None
            if chatPage is None or chatPage.ClassName == 'mmui::ChatPage':
                return None  # 空状态, 无聊天打开

            # 标题栏是 ChatMessagePage 的第一个子控件
            chatPageChildren = chatPage.GetChildren()
            if not chatPageChildren:
                return None
            titleBar = chatPageChildren[0]  # ChatTitleBarMasterView

            # 递归找 XTextView (聊天对象名, 不是群成员数)
            def find_name(ele):
                try:
                    if (ele.ControlTypeName == 'TextControl' and
                        ele.ClassName == 'mmui::XTextView'):
                        name = ele.Name or ''
                        if name and not (name.startswith('(') and name.endswith(')')):
                            return name
                    for c in ele.GetChildren():
                        r = find_name(c)
                        if r:
                            return r
                except:
                    pass
                return None
            return find_name(titleBar)
        except:
            pass
        return None

    def CheckNewMessage(self):
        """检查是否有新消息"""
        self._show()
        return IsRedPixel(self.A_ChatIcon)

    def GetNextNewMessage(self, savepic=False, savefile=False, savevoice=False, timeout=10):
        """获取下一个新消息"""
        msgs = self.GetAllMessage()
        msgids = [i[-1] for i in msgs]

        if not self.usedmsgid:
            self.usedmsgid = msgids

        newmsgids = [i for i in msgids if i not in self.usedmsgid]
        if newmsgids:
            msgList = self._get_msg_list()
            if msgList:
                MsgItems = msgList.GetChildren()
                msgids_full = [''.join([str(k) for k in item.GetRuntimeId()]) for item in MsgItems]
                new = []
                for i in range(len(msgids_full) - 1, -1, -1):
                    if msgids_full[i] in self.usedmsgid:
                        new = msgids_full[i+1:]
                        break
                NewMsgItems = [item for item in MsgItems
                              if ''.join([str(k) for k in item.GetRuntimeId()]) in new
                              and item.ControlTypeName == 'ListItemControl']
                if NewMsgItems:
                    wxlog.debug('获取当前窗口新消息')
                    msgs = self._parse_message_items(NewMsgItems)
                    self.usedmsgid = msgids_full
                    return {self.CurrentChat(): msgs}

        if self.CheckNewMessage():
            wxlog.debug('获取其他窗口新消息')
            t0 = time.time()
            while True:
                if time.time() - t0 > timeout:
                    return {}
                self.A_ChatIcon.DoubleClick(simulateMove=False)
                sessiondict = self.GetSessionList(newmessage=True)
                if sessiondict:
                    break
            for session in sessiondict:
                self.ChatWith(session)
                NewMsgItems = self._get_msg_list().GetChildren()[-sessiondict[session]:]
                msgs = self._parse_message_items(NewMsgItems)
                msgs_all = self.GetAllMessage()
                self.usedmsgid = [i[-1] for i in msgs_all]
                return {session: msgs}
        else:
            wxlog.debug('没有新消息')
            return {}

    def GetAllNewMessage(self, max_round=10):
        """获取所有新消息"""
        newmessages = {}
        for _ in range(max_round):
            newmsg = self.GetNextNewMessage()
            if newmsg:
                for session in newmsg:
                    if session not in newmessages:
                        newmessages[session] = []
                    newmessages[session].extend(newmsg[session])
            else:
                break
        return newmessages

    def SwitchToContact(self):
        """切换到通讯录页面"""
        self._show()
        self.A_ContactsIcon.Click(simulateMove=False)
        time.sleep(0.3)

    def SwitchToChat(self):
        """切换到聊天页面"""
        self._show()
        self.A_ChatIcon.Click(simulateMove=False)
        time.sleep(0.3)

    def AtAll(self, msg=None, who=None):
        """@所有人"""
        if who:
            self.ChatWith(who)
            time.sleep(0.3)

        input_field = self._get_input_field()
        if input_field:
            input_field.SendKeys('@')
            time.sleep(0.2)
            # 查找@所有人弹窗
            atwnd = self.UiaAPI.PaneControl(ClassName='ChatContactMenu')
            if atwnd.Exists(maxSearchSeconds=0.5):
                atwnd.ListItemControl(Name='所有人').Click(simulateMove=False)
                if msg:
                    if not msg.startswith('\n'):
                        msg = '\n' + msg
                    self.SendMsg(msg)
                else:
                    input_field.SendKeys('{Enter}')

    def AddListenChat(self, who, savepic=False, savefile=False, savevoice=False):
        """添加监听对象"""
        self.listen[who] = ChatWnd(who, self.language)
        self.listen[who].savepic = savepic
        self.listen[who].savefile = savefile
        self.listen[who].savevoice = savevoice

    def GetListenMessage(self, who=None):
        """获取监听对象的新消息"""
        if who and who in self.listen:
            chat = self.listen[who]
            return chat.GetNewMessage(
                savepic=chat.savepic, savefile=chat.savefile, savevoice=chat.savevoice
            )
        msgs = {}
        for who_name in self.listen:
            chat = self.listen[who_name]
            msg = chat.GetNewMessage(
                savepic=chat.savepic, savefile=chat.savefile, savevoice=chat.savevoice
            )
            if msg:
                msgs[who_name] = msg
        return msgs

    def RemoveListenChat(self, who):
        """移除监听对象"""
        if who in self.listen:
            del self.listen[who]
        else:
            Warnings.lightred(f'未找到监听对象：{who}', stacklevel=2)

    def GetGroupMembers(self):
        """获取当前聊天群成员"""
        chatPage = self._get_chat_message_page()
        if chatPage is None:
            return []

        try:
            # 点击"聊天信息"按钮
            titleBar = chatPage.GroupControl(ClassName='mmui::ChatTitleBarMasterView')
            infoBtn = titleBar.ButtonControl(Name='聊天信息')
            if infoBtn.Exists(0.1):
                infoBtn.Click(simulateMove=False)
                time.sleep(0.5)

                # 查找聊天信息窗口
                roominfoWnd = self.UiaAPI.Control(
                    ClassName='SessionChatRoomDetailWnd', searchDepth=1
                )
                more = roominfoWnd.ButtonControl(Name='查看更多', searchDepth=8)
                if more.Exists(0.5):
                    more.Click(simulateMove=False)
                    time.sleep(0.3)

                memberList = roominfoWnd.ListControl(Name='聊天成员')
                if memberList.Exists(0.5):
                    members = [m.Name for m in memberList.GetChildren()]
                    while members and members[-1] in ['添加', '移出']:
                        members.pop()
                    roominfoWnd.SendKeys('{Esc}')
                    return members
        except Exception as e:
            wxlog.debug(f'获取群成员失败: {e}')

        return []

    def GetFriendDetails(self, n=None, timeout=0xFFFFF):
        """获取所有好友详情(微信4.x暂不支持)"""
        Warnings.lightred('GetFriendDetails 在微信4.x中暂未适配', stacklevel=2)
        return []

    def GetNewFriends(self):
        """获取新的好友申请列表(微信4.x暂不支持)"""
        Warnings.lightred('GetNewFriends 在微信4.x中暂未适配', stacklevel=2)
        return []

    def GetAllFriends(self, keywords=None):
        """获取所有好友列表(微信4.x暂不支持)"""
        Warnings.lightred('GetAllFriends 在微信4.x中暂未适配', stacklevel=2)
        return []

    def AddNewFriend(self, keywords, addmsg=None, remark=None, tags=None):
        """添加新的好友(微信4.x暂不支持)"""
        Warnings.lightred('AddNewFriend 在微信4.x中暂未适配', stacklevel=2)
        return False

    def GetAllListenChat(self):
        """获取所有监听对象"""
        return self.listen


class ChatWnd(WeChatBase):
    """独立聊天窗口(微信4.x中聊天窗口是主窗口内嵌的，此类用于监听)"""

    def __init__(self, who, language='cn'):
        self.who = who
        self.language = language
        self.usedmsgid = []
        self.savepic = False
        self.savefile = False
        self.savevoice = False

        # 获取微信主窗口并打开此聊天
        self.UiaAPI = uia.WindowControl(ClassName='mmui::MainWindow', searchDepth=1)

    def __repr__(self) -> str:
        return f"<wxauto Chat Window at {hex(id(self))} for {self.who}>"

    def _show(self):
        """激活窗口"""
        try:
            self.UiaAPI.SwitchToThisWindow()
        except:
            pass

    def _get_chat_page(self):
        """获取聊天页面"""
        try:
            rootWidget = self.UiaAPI.GroupControl(searchDepth=1)
            stacked = rootWidget.CustomControl(ClassName='QStackedWidget')
            mainView = stacked.GroupControl(ClassName='mmui::MainView')
            contentWidget = [c for c in mainView.GetChildren()
                           if c.ControlTypeName == 'GroupControl' and c.ClassName == 'QWidget'][0]
            innerMainView = contentWidget.GroupControl(ClassName='mmui::MainView')
            splitter = innerMainView.CustomControl(ClassName='mmui::XSplitterView')
            outerStack = splitter.CustomControl(ClassName='mmui::XStackedWidget')
            outerSplitter = outerStack.CustomControl(ClassName='mmui::XSplitterView')
            rightPanel = [c for c in outerSplitter.GetChildren()
                         if c.ClassName == 'mmui::XStackedWidget'][0]
            chatDetailView = rightPanel.GroupControl(ClassName='mmui::ChatDetailView')
            return chatDetailView.GroupControl(ClassName='mmui::ChatMessagePage')
        except:
            return None

    def _get_msg_list(self):
        chatPage = self._get_chat_page()
        if chatPage is None:
            return None
        try:
            splitter = chatPage.CustomControl(ClassName='mmui::XSplitterView')
            messageView = [c for c in splitter.GetChildren()
                          if c.ClassName == 'mmui::MessageView'][0]
            return messageView.ListControl(ClassName='mmui::RecyclerListView')
        except:
            return None

    def SendMsg(self, msg, at=None):
        wxlog.debug(f"ChatWnd 发送消息：{self.who} --> {msg}")
        self._show()

        chatPage = self._get_chat_page()
        if chatPage is None:
            return

        # 找输入框
        splitter = chatPage.CustomControl(ClassName='mmui::XSplitterView')
        inputArea = [c for c in splitter.GetChildren() if c.ClassName == 'mmui::XView'][0]
        inputGroup = inputArea.GroupControl(ClassName='mmui::InputView')
        xview = inputGroup.GroupControl(ClassName='mmui::XView')
        innerXview = xview.GroupControl(ClassName='mmui::XView')
        editbox = innerXview.EditControl(ClassName='mmui::ChatInputField')

        if at:
            if isinstance(at, str):
                at = [at]
            for person in at:
                editbox.SendKeys('@' + person)
                time.sleep(0.1)
                editbox.SendKeys('{Enter}')
                if msg and not msg.startswith('\n'):
                    msg = '\n' + msg

        if msg:
            t0 = time.time()
            while True:
                if time.time() - t0 > 10:
                    raise TimeoutError(f'发送消息超时 --> {self.who} - {msg}')
                SetClipboardText(msg)
                editbox.SendKeys('{Ctrl}v')
                time.sleep(0.1)
                if editbox.GetValuePattern().Value:
                    break
        editbox.SendKeys('{Enter}')

    def SendFiles(self, filepath):
        Warnings.lightred('SendFiles in ChatWnd 暂未适配', stacklevel=2)
        return False

    def GetAllMessage(self, savepic=False, savefile=False, savevoice=False):
        msgList = self._get_msg_list()
        if msgList is None:
            return []
        items = msgList.GetChildren()
        return self._parse_items(items)

    def GetNewMessage(self, savepic=False, savefile=False, savevoice=False):
        msgList = self._get_msg_list()
        if msgList is None:
            return []

        items = msgList.GetChildren()
        if not self.usedmsgid:
            self.usedmsgid = [''.join([str(k) for k in i.GetRuntimeId()]) for i in items]
            return []

        new_items = [i for i in items
                    if ''.join([str(k) for k in i.GetRuntimeId()]) not in self.usedmsgid]
        if not new_items:
            return []

        new_msgs = self._parse_items(new_items)
        self.usedmsgid = [''.join([str(k) for k in i.GetRuntimeId()]) for i in items]
        return new_msgs

    def _parse_items(self, items):
        msgs = []
        for item in items:
            cls = item.ClassName
            name = item.Name
            rid = ''.join([str(i) for i in item.GetRuntimeId()])

            if cls == 'mmui::ChatItemView':
                msgs.append(TimeMessage(['Time', name, rid], item, self))
            elif cls == 'mmui::ChatTextItemView':
                msgs.append(SelfMessage(['Self', name, rid], item, self))
            elif name:
                msgs.append(SysMessage(['SYS', name, rid], item, self))
        return msgs

    def LoadMoreMessage(self):
        msgList = self._get_msg_list()
        if msgList is None:
            return False
        msgList.WheelUp(wheelTimes=15, waitTime=0.1)
        return True

    def GetGroupMembers(self):
        return []

    def AtAll(self, msg=None):
        editbox = self._get_input_field()
        if editbox:
            editbox.SendKeys('@')
            time.sleep(0.2)
            editbox.SendKeys('{Enter}')

    def Close(self):
        pass


class WeChatImage:
    """微信图片预览(微信4.x暂未适配)"""
    def __init__(self, language='cn') -> None:
        self.language = language

    def __repr__(self) -> str:
        return f"<wxauto WeChat Image at {hex(id(self))}>"

    def Save(self, savepath='', timeout=10):
        Warnings.lightred('WeChatImage.Save 在微信4.x中暂未适配', stacklevel=2)
        return None

    def OCR(self):
        Warnings.lightred('WeChatImage.OCR 在微信4.x中暂未适配', stacklevel=2)
        return ''

    def Previous(self):
        return False

    def Next(self, warning=True):
        return False

    def Close(self):
        pass


class WeChatFiles:
    """微信聊天文件(微信4.x暂未适配)"""
    def __init__(self, language='cn') -> None:
        self.language = language

    def __repr__(self) -> str:
        return f"<wxauto WeChat Files at {hex(id(self))}>"

    def GetSessionList(self, reset=False):
        return []

    def ChatWithFile(self, who):
        raise TargetNotFoundError(f'未查询到目标：{who}')

    def DownloadFiles(self, who, amount, deadline=None, size=None):
        return False

    def Close(self):
        pass
