"""
WeChat 4.1 消息元素与解析类
适配 Qt5/mmui 框架的 UIA 结构
"""
from . import uiautomation as uia
from .languages import *
from .utils import *
from .color import *
from .errors import *
import datetime
import time
import os
import re


class WxParam:
    SYS_TEXT_HEIGHT = 33
    TIME_TEXT_HEIGHT = 34
    RECALL_TEXT_HEIGHT = 45
    CHAT_TEXT_HEIGHT = 52
    CHAT_IMG_HEIGHT = 117
    DEFALUT_SAVEPATH = os.path.join(os.getcwd(), 'wxauto文件')


class WeChatBase:
    """微信基类 — 提供语言支持和消息解析"""
    def _lang(self, text, langtype='MAIN'):
        if langtype == 'MAIN':
            return MAIN_LANGUAGE.get(text, {}).get(self.language, text)
        elif langtype == 'WARNING':
            return WARNING.get(text, {}).get(self.language, text)
        return text

    def _split(self, MsgItem):
        """解析单个消息项 — 微信4.1适配版"""
        uia.SetGlobalSearchTimeout(0)
        MsgItemName = MsgItem.Name
        cls = MsgItem.ClassName
        item_height = MsgItem.BoundingRectangle.height()
        rid = ''.join([str(i) for i in MsgItem.GetRuntimeId()])

        if cls == 'mmui::ChatItemView' or item_height <= WxParam.TIME_TEXT_HEIGHT:
            # 时间标记
            Msg = ['Time', MsgItemName, rid]
        elif '撤回' in MsgItemName:
            Msg = ['Recall', MsgItemName, rid]
        elif cls in ('mmui::ChatTextItemView', 'mmui::ChatFileItemView',
                      'mmui::ChatImageItemView', 'mmui::ChatVideoItemView'):
            # 文本/文件/图片/视频消息
            # 在4.1中, 需要判断消息方向(自已/他人)
            rect = MsgItem.BoundingRectangle
            # 简单策略: 检查是否有子控件来区分群聊中的发送者
            try:
                children = MsgItem.GetChildren()
                if children:
                    # 有子控件的可能是群聊中他人的消息
                    sender_info = children[0].Name if children[0].Name else 'Self'
                    Msg = [(sender_info, sender_info), MsgItemName, rid]
                else:
                    Msg = ['Self', MsgItemName, rid]
            except:
                Msg = ['Self', MsgItemName, rid]
        else:
            # 系统消息或其他
            Msg = ['SYS', MsgItemName, rid]

        uia.SetGlobalSearchTimeout(10.0)
        return ParseMessage(Msg, MsgItem, self)

    def _getmsgs(self, msgitems, savepic=False, savefile=False, savevoice=False):
        """批量解析消息"""
        msgs = []
        for MsgItem in msgitems:
            if MsgItem.ControlTypeName == 'ListItemControl':
                msgs.append(self._split(MsgItem))
        return msgs


class ChatWnd(WeChatBase):
    """独立聊天窗口 - 保留兼容(4.x中聊天窗口内嵌于主窗口)"""

    def __init__(self, who, language='cn'):
        self.who = who
        self.language = language
        self.usedmsgid = []
        self.savepic = False
        self.savefile = False
        self.savevoice = False
        self.UiaAPI = uia.WindowControl(ClassName='mmui::MainWindow', searchDepth=1)

    def __repr__(self) -> str:
        return f"<wxauto Chat Window at {hex(id(self))} for {self.who}>"


class SessionElement:
    """会话列表元素 — 微信4.1适配版

    ChatSessionCell Name格式:
        "会话名\\n[XX条] \\n发送者: 最后一条消息内容\\n时间\\n"
        或 "会话名\\n最后消息\\n时间\\n"
    """

    def __init__(self, item):
        self.element = item
        name_text = item.Name
        lines = name_text.split('\n')

        self.name = lines[0] if lines else ''
        self.isnew = False
        self.time = ''
        self.content = ''

        # 解析未读数
        for line in lines:
            m = re.search(r'\[(\d+)条\]', line)
            if m:
                self.isnew = True
                self.unread = int(m.group(1))
                break
        else:
            self.unread = 0

        # 解析时间(通常最后一行)
        valid_lines = [l for l in lines if l and l != '消息免打扰']
        for i in range(len(valid_lines) - 1, -1, -1):
            if re.match(r'^\d{1,2}:\d{2}$', valid_lines[i]):
                self.time = valid_lines[i]
                # 内容在时间和名字之间
                content_lines = valid_lines[1:i]
                self.content = ' '.join(content_lines)
                break

        if not self.time and len(valid_lines) >= 2:
            self.time = valid_lines[-1]
            content_lines = valid_lines[1:-1]
            self.content = ' '.join(content_lines)

    def __repr__(self) -> str:
        return f"<SessionElement name={self.name!r} unread={self.unread}>"


class NewFriendsElement:
    """新的好友申请(微信4.x暂未适配)"""

    def __init__(self, ele, wx):
        self._wx = wx
        self.ele = ele
        self.name = ''
        self.msg = ''
        self.acceptable = False

    def __repr__(self) -> str:
        return f"<NewFriendsElement {self.name}>"

    def Accept(self, remark=None, tags=None):
        Warnings.lightred('Accept 在微信4.x中暂未适配', stacklevel=2)


class ContactWnd:
    """通讯录管理窗口(微信4.x暂未适配)"""

    def __init__(self):
        pass

    def __repr__(self) -> str:
        return f"<wxauto Contact Window at {hex(id(self))}>"

    def GetFriendNum(self):
        return 0

    def Search(self, keyword):
        pass

    def GetAllFriends(self):
        return []

    def Close(self):
        pass


class ContactElement:
    """通讯录联系人元素"""

    def __init__(self, ele):
        self.element = ele
        self.nickname = ''
        self.remark = ''
        self.tags = []

    def __repr__(self) -> str:
        return f"<ContactElement {self.nickname}>"

    def EditRemark(self, remark: str):
        pass


# ========== 消息类型 ==========

class Message:
    """消息基类"""
    type = 'message'

    def __getitem__(self, index):
        return self.info[index]

    def __str__(self):
        return self.content

    def __repr__(self):
        return str(self.info[:2])


class SysMessage(Message):
    """系统消息"""
    type = 'sys'

    def __init__(self, info, control, wx):
        self.info = info
        self.control = control
        self.wx = wx
        self.sender = info[0]
        self.content = info[1]
        self.id = info[-1]


class TimeMessage(Message):
    """时间标记"""
    type = 'time'

    def __init__(self, info, control, wx):
        self.info = info
        self.control = control
        self.wx = wx
        self.time = ParseWeChatTime(info[1])
        self.sender = info[0]
        self.content = info[1]
        self.id = info[-1]


class RecallMessage(Message):
    """撤回消息"""
    type = 'recall'

    def __init__(self, info, control, wx):
        self.info = info
        self.control = control
        self.wx = wx
        self.sender = info[0]
        self.content = info[1]
        self.id = info[-1]


class SelfMessage(Message):
    """自己发送的消息"""
    type = 'self'

    def __init__(self, info, control, obj):
        self.info = info
        self.control = control
        self._winobj = obj
        self.sender = info[0]
        self.content = info[1]
        self.id = info[-1]
        # chatbox 在4.x中不存在, 需要从obj获取
        self.chatbox = getattr(obj, 'ChatBox', None)

    def quote(self, msg):
        """引用消息(微信4.x暂未适配)"""
        Warnings.lightred('quote 在微信4.x中暂未适配', stacklevel=2)
        return False

    def forward(self, friend):
        """转发消息(微信4.x暂未适配)"""
        Warnings.lightred('forward 在微信4.x中暂未适配', stacklevel=2)
        return False

    def parse(self):
        """解析合并消息(微信4.x暂未适配)"""
        Warnings.lightred('parse 在微信4.x中暂未适配', stacklevel=2)
        return []


class FriendMessage(Message):
    """好友消息"""
    type = 'friend'

    def __init__(self, info, control, obj):
        self.info = info
        self.control = control
        self._winobj = obj

        if isinstance(info[0], (tuple, list)):
            self.sender = info[0][0]
            self.sender_remark = info[0][1]
            self.info[0] = info[0][0]
        else:
            self.sender = info[0]
            self.sender_remark = info[0]

        self.content = info[1]
        self.id = info[-1]
        self.chatbox = getattr(obj, 'ChatBox', None)

    def quote(self, msg):
        Warnings.lightred('quote 在微信4.x中暂未适配', stacklevel=2)
        return False

    def forward(self, friend):
        Warnings.lightred('forward 在微信4.x中暂未适配', stacklevel=2)
        return False

    def parse(self):
        Warnings.lightred('parse 在微信4.x中暂未适配', stacklevel=2)
        return []


class TextElement:
    """文本元素(微信4.x暂未适配)"""

    def __init__(self, ele, wx) -> None:
        self._wx = wx
        self.ele = ele
        self.sender = ''
        self.content = ele.Name if ele else ''
        self.chattype = 'unknown'
        self.chatname = ''
        self.info = {
            'sender': self.sender,
            'content': self.content,
            'chatname': self.chatname,
            'chattype': self.chattype,
            'sender_remark': ''
        }

    def __repr__(self) -> str:
        return f"<TextElement ({self.sender}: {self.content})>"


# ========== 消息解析工厂 ==========

message_types = {
    'SYS': SysMessage,
    'Time': TimeMessage,
    'Recall': RecallMessage,
    'Self': SelfMessage
}


def ParseMessage(data, control, wx):
    """根据消息类型创建对应的Message对象"""
    msg_type = data[0]
    if msg_type in message_types:
        return message_types[msg_type](data, control, wx)
    return FriendMessage(data, control, wx)


class LoginWnd:
    """登录窗口(微信4.x暂未适配)"""
    _class_name = 'WeChatLoginWndForPC'

    def __repr__(self) -> str:
        return f"<wxauto LoginWnd Object at {hex(id(self))}>"

    def login(self):
        pass

    def get_qrcode(self):
        return None
