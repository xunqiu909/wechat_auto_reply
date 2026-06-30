"""
wxauto 4.1 自动回复监控系统 v4
--------------------------------
模式: 检测一次 / 循环检测
多规则: 每规则独立 (聊天+关键词+回复)
预设: 保存/加载/删除 配置快照
"""
import sys, os, json, time, threading, queue, hashlib, copy, ctypes, signal
from ctypes import wintypes
from datetime import datetime

import pyperclip
import win32gui, win32con, win32api
import uiautomation as uia

# ==================== 全局热键 ====================

# Windows API
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

VK_Q     = 0x51
VK_F12   = 0x7B
WM_HOTKEY = 0x0312

class GlobalHotkey:
    """Win32 全局热键 — 用 ctypes 直接调 API"""
    _callbacks = {}
    _next_id = 0
    _running = False

    @classmethod
    def register(cls, modifiers, vk, callback):
        cls._next_id += 1
        hkid = cls._next_id
        cls._callbacks[hkid] = callback

        ok = ctypes.windll.user32.RegisterHotKey(None, hkid, modifiers, vk)
        if not ok:
            raise RuntimeError('RegisterHotKey failed (err={})'.format(
                ctypes.windll.kernel32.GetLastError()))
        return hkid

    @classmethod
    def unregister(cls, hkid):
        ctypes.windll.user32.UnregisterHotKey(None, hkid)
        cls._callbacks.pop(hkid, None)

    @classmethod
    def pump(cls):
        """在自己的线程里跑, PeekMessage + Dispatch"""
        cls._running = True
        msg = wintypes.MSG()
        while cls._running:
            if ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None,
                                                  0, 0, 1):  # PM_REMOVE=1
                if msg.message == WM_HOTKEY:
                    cb = cls._callbacks.get(msg.wParam)
                    if cb:
                        threading.Thread(target=cb, daemon=True).start()
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.05)

    @classmethod
    def stop(cls):
        cls._running = False


# ==================== COM / 底层工具 ====================

def init_com():
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except ImportError:
        pass

def focus_wechat():
    """强制激活微信窗口 — 无论当前在哪个窗口"""
    hwnd = win32gui.FindWindow(None, '微信')
    if not hwnd:
        hwnd = win32gui.FindWindow('mmui::MainWindow', None)
    if not hwnd:
        r = []
        def cb(h, _):
            if '微信' in win32gui.GetWindowText(h) and win32gui.IsWindowVisible(h):
                r.append(h); return False
            return True
        win32gui.EnumWindows(cb, None)
        if r: hwnd = r[0]
    if not hwnd:
        return 0

    # 最小化则恢复
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    # 强制置顶
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    # 移除置顶(避免一直挡住)
    win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.08)
    return hwnd

def mouse_click(x, y):
    win32api.SetCursorPos((x, y))
    time.sleep(0.02)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.02)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

def get_hwnd():
    return win32gui.FindWindow(None, '微信') or win32gui.FindWindow('mmui::MainWindow', None)


# ==================== UIA 导航 ====================

class FreshNav:
    @staticmethod
    @staticmethod
    def _try_uia(max_retries=2):
        """重试获取UIA窗口, 处理stale引用"""
        for i in range(max_retries):
            try:
                h = get_hwnd()
                if not h: return None
                init_com()
                # 每次重新从HWND创建, 避免缓存
                return uia.ControlFromHandle(h)
            except:
                if i == max_retries - 1: return None
                time.sleep(0.3)
        return None

    @staticmethod
    def _nav_splitter():
        w = FreshNav._try_uia()
        if not w: return None
        try:
            r = w.GroupControl(searchDepth=1)
            s = r.CustomControl(ClassName='QStackedWidget')
            m = s.GroupControl(ClassName='mmui::MainView')
            cw = [c for c in m.GetChildren() if c.ClassName=='QWidget' and c.ControlTypeName=='GroupControl']
            if not cw: return None
            im = cw[0].GroupControl(ClassName='mmui::MainView')
            sp = im.CustomControl(ClassName='mmui::XSplitterView')
            os = sp.CustomControl(ClassName='mmui::XStackedWidget')
            return os.CustomControl(ClassName='mmui::XSplitterView')
        except LookupError:
            # stale, 重试一次
            w = FreshNav._try_uia()
            if not w: return None
            try:
                r = w.GroupControl(searchDepth=1)
                s = r.CustomControl(ClassName='QStackedWidget')
                m = s.GroupControl(ClassName='mmui::MainView')
                cw = [c for c in m.GetChildren() if c.ClassName=='QWidget' and c.ControlTypeName=='GroupControl']
                if not cw: return None
                im = cw[0].GroupControl(ClassName='mmui::MainView')
                sp = im.CustomControl(ClassName='mmui::XSplitterView')
                os = sp.CustomControl(ClassName='mmui::XStackedWidget')
                return os.CustomControl(ClassName='mmui::XSplitterView')
            except: return None
        except: return None

    @staticmethod
    def get_message_items():
        sp = FreshNav._nav_splitter()
        if not sp: return None, []
        rights = [c for c in sp.GetChildren() if c.ClassName=='mmui::XStackedWidget']
        if not rights: return None, []
        cd = rights[0].GroupControl(ClassName='mmui::ChatDetailView')
        cp = cd.GetFirstChildControl()
        if cp.ClassName != 'mmui::ChatMessagePage': return None, []
        tb = cp.GetChildren()[0]
        def find_name(ele):
            try:
                if ele.ControlTypeName=='TextControl' and ele.ClassName=='mmui::XTextView':
                    n = ele.Name or ''
                    if n and not (n.startswith('(') and n.endswith(')')): return n
                for c in ele.GetChildren():
                    r = find_name(c)
                    if r: return r
            except: pass
            return None
        chat_name = find_name(tb)
        split = cp.CustomControl(ClassName='mmui::XSplitterView')
        mvs = [c for c in split.GetChildren() if c.ClassName=='mmui::MessageView']
        if not mvs: return chat_name, []
        lst = mvs[0].ListControl(ClassName='mmui::RecyclerListView')
        return chat_name, lst.GetChildren()

    @staticmethod
    def get_search_edit():
        """返回搜索框 EditControl"""
        sp = FreshNav._nav_splitter()
        if not sp: return None
        lefts = [c for c in sp.GetChildren() if c.ClassName=='mmui::XView']
        if not lefts: return None
        cmv = lefts[0].GroupControl(ClassName='mmui::ChatMasterView')
        cl = cmv.GroupControl(ClassName='mmui::XView')
        sf = cl.GroupControl(ClassName='mmui::XSearchField')
        return sf.EditControl(ClassName='mmui::XValidatorTextEdit')

    @staticmethod
    def get_session_table():
        sp = FreshNav._nav_splitter()
        if not sp: return None
        lefts = [c for c in sp.GetChildren() if c.ClassName=='mmui::XView']
        if not lefts: return None
        cmv = lefts[0].GroupControl(ClassName='mmui::ChatMasterView')
        cl = cmv.GroupControl(ClassName='mmui::XView')
        ar = [c for c in cl.GetChildren() if c.ClassName=='mmui::XView']
        if len(ar)<2: return None
        li = ar[1].GroupControl(ClassName='mmui::XView')
        sl = li.GroupControl(ClassName='mmui::ChatSessionList')
        return sl.ListControl(ClassName='mmui::XTableView')

    @staticmethod
    def get_input_field():
        """返回输入框 EditControl (UIA方式)"""
        sp = FreshNav._nav_splitter()
        if not sp: return None
        rights = [c for c in sp.GetChildren() if c.ClassName=='mmui::XStackedWidget']
        if not rights: return None
        cd = rights[0].GroupControl(ClassName='mmui::ChatDetailView')
        cp = cd.GetFirstChildControl()
        if cp.ClassName != 'mmui::ChatMessagePage': return None
        split = cp.CustomControl(ClassName='mmui::XSplitterView')
        xvs = [c for c in split.GetChildren() if c.ClassName=='mmui::XView']
        if not xvs: return None
        ig = xvs[0].GroupControl(ClassName='mmui::InputView')
        xv2 = ig.GroupControl(ClassName='mmui::XView')
        ixv = xv2.GroupControl(ClassName='mmui::XView')
        return ixv.EditControl(ClassName='mmui::ChatInputField')

    @staticmethod
    def get_input_rect():
        sp = FreshNav._nav_splitter()
        if not sp: return None
        rights = [c for c in sp.GetChildren() if c.ClassName=='mmui::XStackedWidget']
        if not rights: return None
        cd = rights[0].GroupControl(ClassName='mmui::ChatDetailView')
        cp = cd.GetFirstChildControl()
        if cp.ClassName!='mmui::ChatMessagePage': return None
        split = cp.CustomControl(ClassName='mmui::XSplitterView')
        xvs = [c for c in split.GetChildren() if c.ClassName=='mmui::XView']
        if not xvs: return None
        r = xvs[0].BoundingRectangle
        return (r.left, r.top, r.right, r.bottom)


# ==================== 数据模型 ====================

class Rule:
    """单条回复规则"""
    __slots__ = ('chat', 'keyword', 'reply', 'enabled')
    def __init__(self, chat='', keyword='', reply='', enabled=True):
        self.chat = chat
        self.keyword = keyword
        self.reply = reply
        self.enabled = enabled

    def to_dict(self):
        return {'chat': self.chat, 'keyword': self.keyword,
                'reply': self.reply, 'enabled': self.enabled}

    @staticmethod
    def from_dict(d):
        return Rule(d.get('chat',''), d.get('keyword',''),
                    d.get('reply',''), d.get('enabled',True))


class RawMsg:
    __slots__ = ('cls','name','runtime_id')
    def __init__(self, item):
        self.cls = item.ClassName
        self.name = item.Name or ''
        try: self.runtime_id = ''.join(str(i) for i in item.GetRuntimeId())
        except: self.runtime_id = ''
    @property
    def is_timestamp(self): return self.cls == 'mmui::ChatItemView'
    @property
    def is_text(self): return self.cls == 'mmui::ChatTextItemView'
    @property
    def content_hash(self):
        return hashlib.md5(self.name.encode('utf-8','replace')).hexdigest()[:16]


# ==================== 监控引擎 ====================

class MonitorEngine:
    """自动回复引擎 — 支持 once / loop 模式"""

    def __init__(self):
        self.mode = 'once'           # 'once' | 'loop'
        self.rules = []              # [Rule, ...]
        self.poll_interval = 3       # 单次轮询间隔(秒)
        self.active_duration = 30    # 循环模式: 每次激活持续秒数
        self.pause_duration = 0      # 循环模式: 每次暂停秒数(0=不休)
        self.monitor_running = False
        self._thread = None
        self._lock = threading.Lock()
        self.log_queue = queue.Queue()

        self.baseline_ids = {}
        self.last_incoming = {}   # {chat: text} 上次已回复的 incoming 消息文本
        self.sent_texts = set()   # 自己发过的文本(防循环)
        self.stop_requested = False  # 用于 Ctrl+C / 超时 / 手动停止

        self.status = {
            'mode': '检测一次', 'current_chat': '', 'last_message': '',
            'last_trigger': '', 'last_send': '', 'round': 0, 'total_rounds': 0,
        }

        self.config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'auto_reply_config.json')
        self.presets_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'auto_reply_presets.json')

    def log(self, msg):
        t = datetime.now().strftime('%H:%M:%S')
        text = '[{}] {}'.format(t, msg)
        self.log_queue.put(text)
        try: print(text)
        except: print(text.encode('ascii','replace').decode('ascii','replace'))

    # ---- 配置读写 ----

    def load_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.mode = data.get('mode', 'once')
                self.poll_interval = data.get('poll_interval', 3)
                self.active_duration = data.get('active_duration', 30)
                self.pause_duration = data.get('pause_duration', 0)
                self.rules = [Rule.from_dict(r) for r in data.get('rules', [])]
                self.log('已加载配置: {}模式, {} 条规则'.format(
                    '循环' if self.mode=='loop' else '单次', len(self.rules)))
                return True
            except Exception as e:
                self.log('加载配置失败: {}'.format(e))
        return False

    def save_config(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'mode': self.mode,
                    'poll_interval': self.poll_interval,
                    'active_duration': self.active_duration,
                    'pause_duration': self.pause_duration,
                    'rules': [r.to_dict() for r in self.rules],
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log('保存配置失败: {}'.format(e))

    # ---- 预设管理 ----

    def load_presets(self):
        if os.path.exists(self.presets_path):
            try:
                with open(self.presets_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except: pass
        return {}

    def save_presets(self, presets):
        try:
            with open(self.presets_path, 'w', encoding='utf-8') as f:
                json.dump(presets, f, ensure_ascii=False, indent=2)
        except: pass

    def save_preset(self, name):
        presets = self.load_presets()
        presets[name] = {
            'mode': self.mode,
            'poll_interval': self.poll_interval,
            'active_duration': self.active_duration,
            'pause_duration': self.pause_duration,
            'rules': [r.to_dict() for r in self.rules],
        }
        self.save_presets(presets)
        self.log('预设已保存: {}'.format(name))

    def load_preset(self, name):
        presets = self.load_presets()
        if name not in presets:
            return False
        p = presets[name]
        self.mode = p.get('mode', 'once')
        self.poll_interval = p.get('poll_interval', 3)
        self.active_duration = p.get('active_duration', 30)
        self.pause_duration = p.get('pause_duration', 0)
        self.rules = [Rule.from_dict(r) for r in p.get('rules', [])]
        self.save_config()
        self.log('加载预设: {} ({}模式, {}条规则)'.format(name,
            '循环' if self.mode=='loop' else '单次', len(self.rules)))
        return True

    def delete_preset(self, name):
        presets = self.load_presets()
        if name in presets:
            del presets[name]
            self.save_presets(presets)
            return True
        return False

    # ---- 规则 CRUD ----

    def add_rule(self, chat, keyword, reply):
        with self._lock:
            self.rules.append(Rule(chat, keyword, reply, True))
            self.save_config()
            self.log('添加规则: {} | {} | {}...'.format(chat, keyword, reply[:25]))

    def remove_rule(self, index):
        with self._lock:
            if 0 <= index < len(self.rules):
                r = self.rules.pop(index)
                self.save_config()
                self.log('删除规则: {} | {}'.format(r.chat, r.keyword))

    def toggle_rule(self, index, enabled=None):
        with self._lock:
            if 0 <= index < len(self.rules):
                if enabled is None:
                    self.rules[index].enabled = not self.rules[index].enabled
                else:
                    self.rules[index].enabled = enabled
                self.save_config()

    # ---- 核心操作 ----

    def _open_chat(self, who):
        """打开指定会话: 可见列表→翻页→搜索框"""
        try:
            return self.__open_chat(who)
        except (LookupError, Exception) as e:
            self.log('[ERR] _open_chat失败: {}'.format(str(e)[:50]))
            return False

    def __open_chat(self, who):
        init_com()
        uia.SetGlobalSearchTimeout(2)

        # 快速路径: 当前已是目标聊天(不切窗口)
        try:
            cur_name, _ = FreshNav.get_message_items()
            if cur_name and cur_name.strip() == who.strip():
                self.status['current_chat'] = cur_name
                return True
        except: pass

        # 需要切换聊天 → 切前台
        focus_wechat()
        time.sleep(0.05)

        # ---- 方法1: 可见列表 + 翻页 ----
        table = FreshNav.get_session_table()
        if not table:
            return False

        # 先滚到顶
        for _ in range(25):
            table.WheelUp(wheelTimes=20, waitTime=0.002, interval=0.002)
        time.sleep(0.15)

        seen = set()
        for scan in range(30):
            table = FreshNav.get_session_table()
            if not table: break
            items = table.GetChildren()
            cur = set()
            for item in items:
                n = item.Name.split('\n')[0]
                cur.add(n)
                if who == n:
                    r = item.BoundingRectangle
                    mouse_click((r.left+r.right)//2, (r.top+r.bottom)//2)
                    time.sleep(0.2)
                    return FreshNav._nav_splitter() is not None
            if cur.issubset(seen): break
            seen.update(cur)
            # 慢速翻: 每次6个 wheel, 间隔更长
            table.WheelDown(wheelTimes=6, waitTime=0.02, interval=0.015)
            time.sleep(0.2)

        # ---- 方法2: 搜索框 ----
        self.log('[SEARCH] 滚动未找到 {}, 用搜索框'.format(who[:10]))
        search_edit = FreshNav.get_search_edit()
        if not search_edit:
            return False

        # 先点击搜索框获得焦点
        search_edit.Click(simulateMove=False)
        time.sleep(0.1)

        # 清空
        search_edit.SendKeys('{Ctrl}a{Back}', waitTime=0.05)
        time.sleep(0.05)

        # 方式1: ValuePattern.SetValue(最可靠)
        try:
            vp = search_edit.GetValuePattern()
            if vp:
                vp.SetValue(who)
                time.sleep(0.1)
                # 触发一下搜索过滤(发送一个key event触发list刷新)
                search_edit.SendKeys('{End}', waitTime=0.05)
                time.sleep(0.05)
                search_edit.SendKeys('{Back}', waitTime=0.05)
                # 再设置一次确保
                time.sleep(0.1)
                if vp.Value != who:
                    vp.SetValue(who)
                    time.sleep(0.1)
                self.log('[SEARCH] ValuePattern set: {!r}'.format(vp.Value[:20]))
        except Exception as e:
            self.log('[SEARCH] ValuePattern失败: {}, 用剪贴板'.format(e))
            # 方式2: 剪贴板粘贴
            pyperclip.copy(who)
            time.sleep(0.05)
            search_edit.SendKeys('{Ctrl}v', waitTime=0.05)

        time.sleep(0.8)

        # 获取搜索过滤后的列表
        table = FreshNav.get_session_table()
        if not table:
            self.log('[SEARCH] 搜索后无法获取会话列表')
            try: search_edit.SendKeys('{Ctrl}a{Back}')
            except: pass
            return False

        items = table.GetChildren()

        # 如果列表还是空的, 等一等再试
        if not items:
            time.sleep(0.5)
            table = FreshNav.get_session_table()
            if table:
                items = table.GetChildren()

        if not items:
            self.log('[SEARCH] 搜索结果为空, 没有对应会话')
            try: search_edit.SendKeys('{Ctrl}a{Back}')
            except: pass
            return False

        # 在结果列表中按名称匹配(精确 > 包含 > 第一条)
        best = None
        for item in items:
            n = item.Name.split('\n')[0]
            if who == n:
                best = item
                break
            if who in n or n in who:
                if best is None:
                    best = item

        if best is None:
            self.log('[SEARCH] 搜索结果({}条)无名称匹配: {}'.format(len(items), [i.Name.split(chr(10))[0][:20] for i in items[:5]]))
            try: search_edit.SendKeys('{Ctrl}a{Back}')
            except: pass
            return False

        n = best.Name.split('\n')[0]
        self.log('[SEARCH] 点击: {}'.format(n[:40]))
        r = best.BoundingRectangle
        mouse_click((r.left+r.right)//2, (r.top+r.bottom)//2)
        time.sleep(0.2)

        # 清除搜索框
        try:
            search_edit.SendKeys('{Ctrl}a{Back}')
        except: pass
        time.sleep(0.1)

        return FreshNav._nav_splitter() is not None

    def _send_reply(self, text):
        # 抢焦点到微信
        focus_wechat()
        time.sleep(0.2)  # 等窗口真正拿到焦点
        init_com()

        # 获取输入框坐标点击
        rect = FreshNav.get_input_rect()
        if not rect:
            # 回退: 直接用 UIA 找 ChatInputField
            self.log('[SEND] 坐标取输入框失败,尝试UIA定位')
            try:
                input_field = FreshNav.get_input_field()
                if input_field:
                    input_field.Click(simulateMove=False)
                    time.sleep(0.1)
                    pyperclip.copy(text)
                    time.sleep(0.05)
                    input_field.SendKeys('{Ctrl}v', waitTime=0.03)
                    time.sleep(0.08)
                    input_field.SendKeys('{Enter}', waitTime=0.03)
                    time.sleep(0.08)
                    self.sent_texts.add(text.strip())
                    self.log('[SEND] 成功(UIA)')
                    return True
            except Exception as e:
                self.log('[SEND] UIA也失败: {}'.format(e))
            return False

        mouse_click((rect[0]+rect[2])//2, (rect[1]+rect[3])//2)
        time.sleep(0.12)

        # 粘贴
        pyperclip.copy(text)
        time.sleep(0.08)
        uia.SendKeys('{Ctrl}v', waitTime=0.05)
        time.sleep(0.12)

        # 发送
        uia.SendKeys('{Enter}', waitTime=0.05)
        time.sleep(0.12)

        self.sent_texts.add(text.strip())
        self.log('[SEND] 成功')
        return True

    def _is_self(self, text):
        t = text.strip()
        if t in self.sent_texts: return True
        for s in self.sent_texts:
            if s.strip() == t: return True
        return False

    @staticmethod
    def _normalize(t):
        if not t: return ''
        return t.strip().replace('\n','').replace(' ','')

    @staticmethod
    def _match_kw(msg_text, keyword):
        if not msg_text or not keyword: return False
        m = MonitorEngine._normalize(msg_text)
        k = MonitorEngine._normalize(keyword)
        if not k: return False
        return k in m or m == k

    # ---- 运行入口 ----

    def start(self, mode=None):
        """启动监控。mode=None使用已配置的模式"""
        if self.monitor_running:
            self.log('[START] 已在运行中')
            return False
        if mode is not None:
            self.mode = mode
        self._validate_mode()
        focus_wechat()
        time.sleep(0.3)
        self.baseline_ids.clear()
        self.last_incoming.clear()
        self.sent_texts.clear()
        self.stop_requested = False
        self.status = {
            'mode': '循环检测' if self.mode=='loop' else '检测一次',
            'current_chat': '', 'last_message': '',
            'last_trigger': '', 'last_send': '',
            'round': 0, 'total_rounds': 0,
        }
        self.monitor_running = True
        self._thread = threading.Thread(target=self._runner, daemon=True)
        self._thread.start()

        # 注册全局热键 Ctrl+Shift+C 强制停止(控制台Ctrl+C也有效)
        if not hasattr(MonitorEngine, '_hotkey_hkid'):
            try:
                _stop_self = self.stop
                MonitorEngine._hotkey_hkid = GlobalHotkey.register(
                    MOD_CONTROL | MOD_SHIFT, 0x43,  # VK_C
                    lambda: _stop_self())
                threading.Thread(target=GlobalHotkey.pump, daemon=True).start()
                self.log('[HOTKEY] Ctrl+Shift+C 全局强制停止已注册')
            except Exception as e:
                self.log('[HOTKEY] 注册失败: {}'.format(e))

        # 控制台 Ctrl+C 信号处理
        try:
            signal.signal(signal.SIGINT, lambda sig, frame: self.stop())
        except: pass

        if self.mode == 'loop':
            self.log('[START] 模式: 循环 | 激活{}秒/暂停{}秒 | 间隔{}秒 | 规则: {}条'.format(
                self.active_duration, self.pause_duration, self.poll_interval,
                sum(1 for r in self.rules if r.enabled)))
        else:
            self.log('[START] 模式: 单次 | 规则: {}条'.format(
                sum(1 for r in self.rules if r.enabled)))
        return True

    def stop(self):
        self.stop_requested = True
        if not self.monitor_running: return
        self.monitor_running = False
        if self._thread:
            self._thread.join(timeout=5)
        self.log('[STOP] 监控已停止 (完成{}轮)'.format(self.status['round']))

    def _validate_mode(self):
        if self.mode not in ('once', 'loop'):
            self.mode = 'once'

    def _runner(self):
        init_com()
        uia.SetGlobalSearchTimeout(3)
        self._validate_mode()
        self.log('[RUN] {}模式启动'.format('循环' if self.mode=='loop' else '单次'))
        t0 = time.time()

        if self.mode == 'once':
            self._poll_once()
        else:
            self._loop_forever(t0)

        self.monitor_running = False
        elapsed = int(time.time() - t0)
        self.log('[END] 运行结束，耗时 {} 秒，共 {} 轮'.format(elapsed, self.status['round']))

    def _poll_once(self):
        """单次检测: 遍历所有规则一次，匹配则回复，然后停止"""
        rules = [r for r in self.rules if r.enabled]
        if not rules:
            self.log('[SKIP] 没有启用的规则')
            return
        self.status['total_rounds'] = 1
        self._poll_all_rules(rules, is_once=True)

    def _loop_forever(self, start_time):
        """循环检测: 激活N秒 → 暂停N秒 → 重复，直到手动停止"""
        active_dur = self.active_duration
        pause_dur = self.pause_duration
        self.log('[LOOP] 激活{}秒 / 暂停{}秒 循环中...'.format(active_dur, pause_dur))

        cycle = 0
        while not self.stop_requested:
            cycle += 1
            rules = [r for r in self.rules if r.enabled]

            # ==== 激活阶段 ====
            phase_start = time.time()
            self.log('[LOOP] 周期#{} 激活开始(最长{}秒)'.format(cycle, active_dur))

            while not self.stop_requested:
                elapsed = time.time() - phase_start
                if elapsed >= active_dur: break

                if not rules:
                    time.sleep(0.3)
                    continue

                try:
                    self.status['round'] += 1
                    self.status['total_rounds'] = '-'
                    self._poll_all_rules(rules, is_once=False)
                    time.sleep(max(self.poll_interval, 0.1))
                except (LookupError, Exception) as e:
                    self.log('[ERR] 本轮异常: {}'.format(str(e)[:60]))
                    time.sleep(1)

            # ==== 暂停阶段 ====
            if pause_dur > 0 and not self.stop_requested:
                self.log('[LOOP] 周期#{} 暂停{}秒'.format(cycle, pause_dur))
                # 分小段sleep, 以便能响应stop
                slept = 0
                while slept < pause_dur and not self.stop_requested:
                    time.sleep(1)
                    slept += 1

    def _poll_all_rules(self, rules, is_once):
        """
        每轮检测: 按聊天分组, 每个聊天只看最后一条 incoming,
        匹配任一条规则 → 回复该规则的回复内容 → 本条聊天结束。
        """
        grouped = {}
        for idx, rule in enumerate(rules):
            grouped.setdefault(rule.chat, []).append((idx, rule))

        for chat, rule_list in grouped.items():
            if self.stop_requested: break

            # 1. 打开聊天
            if not self._open_chat(chat):
                self.log('[SKIP] 规则#{} 无法打开: {}'.format(rule_list[0][0]+1, chat))
                continue
            time.sleep(0.1)

            # 2. 取最新一条 incoming (后台检测, 不切窗口)
            cur_name, items = FreshNav.get_message_items()
            if not cur_name or cur_name.strip() != chat.strip():
                self.log('[SKIP] 聊天不匹配: 期望[{}] 实际[{}]'.format(chat, cur_name))
                continue

            # 取最后一条文本消息(包含自己发的, 用于刷新去重)
            latest = None
            for item in reversed(list(items)):
                msg = RawMsg(item)
                if msg.is_timestamp or not msg.name.strip():
                    continue
                latest = msg
                break

            if latest is None:
                self.log('[POLL] 聊天: {} 无消息'.format(chat))
                continue

            # 解析发送人 (群聊: "sender_id\ncontent", 单聊: 纯 "content")
            full = latest.name
            sender = ''
            content = full
            if '\n' in full and len(full.split('\n')[0]) < 30:
                parts = full.split('\n', 1)
                sender = parts[0]
                content = parts[1] if len(parts) > 1 else full

            is_self = self._is_self(full)
            if is_self:
                self.log('[POLL] 聊天: {} 最后消息: {!r} (自己)'.format(chat, full[:50]))
            elif sender:
                self.log('[POLL] 聊天: {} 发送人:{} 最后消息: {!r}'.format(chat, sender, content[:50]))
            else:
                self.log('[POLL] 聊天: {} 最后消息: {!r}'.format(chat, content[:60]))

            # 3. 和上次相同(聊天+发送人+文本) → 跳过
            dedup_key = '{}|{}|{}'.format(chat, sender, content.strip())
            prev = self.last_incoming.get(chat, '')
            if dedup_key == prev:
                self.log('[DEDUP] 最后消息未变 ({}) -> SKIP'.format(prev[:40]))
                continue
            self.last_incoming[chat] = dedup_key

            self.status['current_chat'] = chat
            self.status['last_message'] = content[:60]

            # 4. 自己发的不触发规则, 但已刷新去重
            if is_self:
                continue

            # 5. 逐条规则匹配(对 content 做关键词匹配)
            for idx, rule in rule_list:
                if not self._match_kw(content, rule.keyword):
                    continue
                if not rule.reply.strip():
                    continue
                if rule.reply.strip() in full:
                    self.log('[SKIP] 防循环: {!r}'.format(rule.reply[:20]))
                    continue

                self.log('[KEYWORD] 触发! [{}] kw={!r}'.format(chat, rule.keyword))
                ok = self._send_reply(rule.reply)
                if ok:
                    self.log('[REPLY] -> [{}]: {}'.format(chat, rule.reply[:40]))
                    self.status['last_trigger'] = '{} | {}'.format(chat, rule.keyword)
                    self.status['last_send'] = '成功'
                else:
                    self.status['last_send'] = '失败'
                break  # 一条匹配即止


# ==================== Tkinter UI ====================

class AutoReplyUI:

    C = {'bg':'#F5F7FA','card':'#FFFFFF','header':'#24292E',
         'primary':'#07C160','primary_hov':'#06AD56','blue':'#1677FF',
         'blue_hov':'#4096FF','danger':'#F5222D','danger_hov':'#FF4D4F',
         'warning':'#FAAD14','text':'#1F2937','text2':'#6B7280',
         'border':'#E5E7EB','row_even':'#FAFBFC','row_hover':'#E8F5E9',}

    def __init__(self, engine):
        self.engine = engine
        engine.load_config()
        self.root = None
        self.log_text = None

    def _btn(self, parent, text, cmd, color, hcolor, w=11, fs=10):
        tk = self.tk; c=self.C.get(color,color); hc=self.C.get(hcolor,hcolor)
        b = tk.Frame(parent, bg=self.C['border'], cursor='hand2', padx=1, pady=1)
        i = tk.Frame(b, bg=c); i.pack(fill=tk.BOTH, expand=True)
        l = tk.Label(i, text=text, bg=c, fg='white',
                     font=('Microsoft YaHei UI', fs, 'bold'), padx=w, pady=4)
        l.pack()
        def cb(e): cmd()
        def en(e): i.configure(bg=hc); l.configure(bg=hc)
        def le(e): i.configure(bg=c); l.configure(bg=c)
        for w in (b,i,l): w.bind('<Button-1>',cb); w.bind('<Enter>',en); w.bind('<Leave>',le)
        return b

    def _bsm(self, parent, text, cmd, color, hcolor):
        tk = self.tk; c=self.C.get(color,color); hc=self.C.get(hcolor,hcolor)
        b = tk.Frame(parent, bg=self.C['border'], cursor='hand2', padx=1, pady=1)
        i = tk.Frame(b, bg=c); i.pack(fill=tk.BOTH, expand=True)
        l = tk.Label(i, text=text, bg=c, fg='white',
                     font=('Microsoft YaHei UI', 9, 'bold'), padx=10, pady=3)
        l.pack()
        def cb(e): cmd()
        def en(e): i.configure(bg=hc); l.configure(bg=hc)
        def le(e): i.configure(bg=c); l.configure(bg=c)
        for w in (b,i,l): w.bind('<Button-1>',cb); w.bind('<Enter>',en); w.bind('<Leave>',le)
        return b

    def _card(self, parent, title=None, px=10, py=10):
        tk = self.tk
        outer = tk.Frame(parent, bg=self.C['border'], padx=2, pady=2)
        inner = tk.Frame(outer, bg=self.C['card'], padx=px, pady=py)
        inner.pack(fill=tk.BOTH, expand=True)
        if title:
            h = tk.Frame(inner, bg=self.C['card'], height=26); h.pack(fill=tk.X, pady=(0,5))
            h.pack_propagate(False)
            tk.Label(h, text=title, bg=self.C['card'], fg=self.C['text'],
                     font=('Microsoft YaHei UI', 10, 'bold')).pack(side=tk.LEFT)
            tk.Frame(h, bg=self.C['border'], height=1).pack(side=tk.BOTTOM, fill=tk.X)
        return outer, inner

    def _lbl(self, parent, text):
        tk = self.tk
        tk.Label(parent, text=text, bg=self.C['card'], fg=self.C['text2'],
                 font=('Microsoft YaHei UI', 9), anchor='w').pack(fill=tk.X, pady=(6,3))

    def _ent(self, parent):
        tk = self.tk
        e = tk.Entry(parent, font=('Microsoft YaHei UI', 10), relief=tk.FLAT, bd=0,
                     bg='#F9FAFB', fg=self.C['text'], insertbackground='#07C160')
        e.pack(fill=tk.X, ipady=4, pady=(0,2))
        return e

    def build_and_run(self):
        try:
            import tkinter as tk
            from tkinter import ttk, messagebox, simpledialog
            import tkinter.scrolledtext as st
        except Exception as e:
            print('Tkinter not available: {}'.format(e))
            self._console_mode()
            return
        self.tk = tk; self.ttk = ttk; self.messagebox = messagebox
        self.simpledialog = simpledialog; self.scrolledtext = st

        self.root = tk.Tk()
        self.root.title('wxauto 自动回复监控')
        self.root.geometry('900x780')
        self.root.minsize(820, 600)
        self.root.configure(bg=self.C['bg'])

        self._build_ui()
        self._refresh_all()
        self._update_status()
        self._pull_logs()

        self.root.protocol('WM_DELETE_WINDOW',
            lambda: (self.engine.stop(), self.root.destroy()))

        def force_stop(e=None):
            self.engine.log('[CTRL+C] 强制中断'); self.engine.stop()
        self.root.bind_all('<Control-c>', force_stop)
        try:
            import signal
            signal.signal(signal.SIGINT, lambda sig, frame: force_stop())
        except: pass

        self.root.mainloop()

    def _build_ui(self):
        tk = self.tk

        # ========== HEADER ==========
        hdr = tk.Frame(self.root, bg=self.C['header'], padx=16, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text='wxauto 自动回复监控', bg=self.C['header'], fg='white',
                 font=('Microsoft YaHei UI', 14, 'bold')).pack(side=tk.LEFT)
        tk.Label(hdr, text='微信消息关键词自动回复工具', bg=self.C['header'],
                 fg='#9CA3AF', font=('Microsoft YaHei UI', 9)).pack(side=tk.LEFT, padx=12)
        self.header_badge = tk.Label(hdr, text='已停止', bg=self.C['danger'], fg='white',
                                     font=('Microsoft YaHei UI', 9, 'bold'), padx=10, pady=2)
        self.header_badge.pack(side=tk.RIGHT)

        # ========== PARAMS CARD ==========
        pc_out, pc = self._card(self.root)
        pc_out.pack(fill=tk.X, padx=10, pady=(8, 0))
        r1 = tk.Frame(pc, bg=self.C['card']); r1.pack(fill=tk.X, pady=(0, 4))
        tk.Label(r1, text='运行模式', bg=self.C['card'], fg=self.C['text'],
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(side=tk.LEFT, padx=(0, 10))
        self.mode_var = tk.StringVar(value=self.engine.mode)
        for txt, val in [('检测一次', 'once'), ('循环检测', 'loop')]:
            tk.Radiobutton(r1, text=txt, variable=self.mode_var, value=val,
                           command=self._on_mode_change, bg=self.C['card'], fg=self.C['text'],
                           activebackground=self.C['card'],
                           font=('Microsoft YaHei UI', 10)).pack(side=tk.LEFT, padx=5)
        self.loop_params_frame = tk.Frame(r1, bg=self.C['card'])
        self.loop_params_frame.pack(side=tk.LEFT, padx=15)
        for label, name, attr in [('激活秒', 'entry_active', 'active_duration'),
                                   ('暂停秒', 'entry_pause', 'pause_duration'),
                                   ('间隔秒', 'entry_interval', 'poll_interval')]:
            tk.Label(self.loop_params_frame, text=label, bg=self.C['card'],
                     fg=self.C['text2'], font=('Microsoft YaHei UI', 8)).pack(side=tk.LEFT, padx=(6, 2))
            e = tk.Entry(self.loop_params_frame, font=('Consolas', 10), width=5,
                         relief=tk.FLAT, bd=0, bg='#F9FAFB', fg=self.C['text'], justify='center')
            e.pack(side=tk.LEFT); setattr(self, name, e)
            e.insert(0, str(getattr(self.engine, attr)))
        self._bsm(self.loop_params_frame, '应用参数', self._apply_loop_params, 'blue', 'blue_hov').pack(side=tk.LEFT, padx=8)
        pr = tk.Frame(r1, bg=self.C['card']); pr.pack(side=tk.RIGHT)
        self._bsm(pr, '保存预设', self._save_preset, 'text2', 'text2').pack(side=tk.LEFT, padx=3)
        self._bsm(pr, '加载预设', self._load_preset, 'text2', 'text2').pack(side=tk.LEFT, padx=3)
        self._bsm(pr, '删除预设', self._delete_preset, 'text2', 'text2').pack(side=tk.LEFT, padx=3)
        self._update_loop_params_visibility()

        # ========== MAIN: LEFT + RIGHT ==========
        main = tk.Frame(self.root, bg=self.C['bg'])
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        # ---- LEFT CARD ----
        l_out, lc = self._card(main, '回复规则列表')
        l_out.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        tree_frame = tk.Frame(lc, bg=self.C['card']); tree_frame.pack(fill=tk.BOTH, expand=True)
        sty = self.ttk.Style(); sty.theme_use('clam')
        sty.configure('R.Treeview', font=('Microsoft YaHei UI', 9), rowheight=28,
                      background=self.C['card'], fieldbackground=self.C['card'],
                      foreground=self.C['text'], borderwidth=0)
        sty.configure('R.Treeview.Heading', font=('Microsoft YaHei UI', 9, 'bold'),
                      background='#F3F4F6', foreground=self.C['text'], relief='flat', borderwidth=0)
        sty.map('R.Treeview', background=[('selected', self.C['row_hover'])],
                foreground=[('selected', self.C['text'])])
        cols = ('status', 'chat', 'keyword', 'reply')
        self.rules_tree = tk.ttk.Treeview(tree_frame, columns=cols, show='headings',
                                          height=8, selectmode='browse', style='R.Treeview')
        self.rules_tree.heading('status', text='状态'); self.rules_tree.column('status', width=45, anchor='center')
        self.rules_tree.heading('chat', text='聊天对象'); self.rules_tree.column('chat', width=110)
        self.rules_tree.heading('keyword', text='关键词'); self.rules_tree.column('keyword', width=75)
        self.rules_tree.heading('reply', text='回复内容'); self.rules_tree.column('reply', width=130)
        self.rules_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.rules_tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        sb = tk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.rules_tree.yview,
                         bg=self.C['card']); self.rules_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        bf = tk.Frame(lc, bg=self.C['card']); bf.pack(fill=tk.X, pady=(8, 0))
        self._bsm(bf, '删除选中', self._del_rule, 'danger', 'danger_hov').pack(side=tk.LEFT, padx=2)
        self._bsm(bf, '启用/禁用', self._toggle_rule, 'warning', 'warning').pack(side=tk.LEFT, padx=2)
        self._bsm(bf, '全部启用', lambda: self._all_toggle(True), 'primary', 'primary_hov').pack(side=tk.LEFT, padx=2)
        self._bsm(bf, '全部禁用', lambda: self._all_toggle(False), 'text2', 'text2').pack(side=tk.LEFT, padx=2)
        # status panel
        s_out, sc = self._card(lc, '运行状态'); s_out.pack(fill=tk.X, pady=(8, 0))
        self.status_labels = {}
        for key, label in [('monitor','监控状态'),('mode','运行模式'),('current_chat','当前聊天'),
                           ('last_message','最后检测消息'),('last_trigger','最后触发'),('last_send','最后发送')]:
            row = tk.Frame(sc, bg=self.C['card']); row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=label+':', bg=self.C['card'], fg=self.C['text2'],
                     font=('Microsoft YaHei UI', 8, 'bold'), width=12, anchor='e').pack(side=tk.LEFT)
            val = tk.Label(row, text='-', bg=self.C['card'], fg=self.C['text'],
                           font=('Microsoft YaHei UI', 8), anchor='w')
            val.pack(side=tk.LEFT, padx=6); self.status_labels[key] = val
        self.hint_label = tk.Label(sc, text='', bg=self.C['card'], fg=self.C['warning'],
                                   font=('Microsoft YaHei UI', 8), wraplength=300)
        self.hint_label.pack(fill=tk.X, pady=(5, 0))

        # ---- RIGHT CARD ----
        r_out, rc = self._card(main, '添加 / 编辑规则')
        r_out.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(5, 0))
        self._lbl(rc, '聊天对象名'); self.entry_chat = self._ent(rc)
        self._lbl(rc, '触发关键词'); self.entry_keyword = self._ent(rc)
        self._lbl(rc, '自动回复内容')
        tf2 = tk.Frame(rc, bg=self.C['border'], padx=1, pady=1); tf2.pack(fill=tk.X, pady=(0, 5))
        self.text_reply = tk.Text(tf2, font=('Microsoft YaHei UI', 10), height=5,
                                  relief=tk.FLAT, bd=0, wrap=tk.WORD, bg='#F9FAFB',
                                  fg=self.C['text'], insertbackground='#07C160')
        self.text_reply.pack(fill=tk.X)
        ebf = tk.Frame(rc, bg=self.C['card']); ebf.pack(fill=tk.X, pady=(8, 0))
        self._btn(ebf, '添加规则', self._add_rule, 'primary', 'primary_hov', w=10).pack(side=tk.LEFT, padx=2)
        self._btn(ebf, '更新选中', self._update_rule, 'blue', 'blue_hov', w=10).pack(side=tk.LEFT, padx=4)
        self._btn(ebf, '清空', self._clear_form, 'text2', 'text2', w=8, fs=9).pack(side=tk.LEFT, padx=4)

        # ========== BOTTOM: control + log ==========
        bottom = tk.Frame(self.root, bg=self.C['bg'])
        bottom.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        c_out, cc = self._card(bottom); c_out.pack(fill=tk.X, pady=(0, 6))
        ci = tk.Frame(cc, bg=self.C['card']); ci.pack(fill=tk.X, pady=2)
        self.btn_start_once = self._btn(ci, '检测一次', self._start_once, 'blue', 'blue_hov', w=11)
        self.btn_start_loop = self._btn(ci, '开始循环检测', self._start_loop, 'primary', 'primary_hov', w=11)
        self.btn_stop = self._btn(ci, '停止监控', self._stop, 'danger', 'danger_hov', w=11)
        self.btn_stop.pack_forget()
        self.led = tk.Label(ci, text='O 已停止', font=('Microsoft YaHei UI', 11, 'bold'),
                            bg=self.C['card'], fg=self.C['danger']); self.led.pack(side=tk.LEFT, padx=15)
        self.round_label = tk.Label(ci, text='', font=('Microsoft YaHei UI', 9),
                                    bg=self.C['card'], fg=self.C['text2'])
        self.round_label.pack(side=tk.LEFT, padx=10)
        self._update_start_button()

        # Log
        _, lgc = self._card(bottom, '运行日志'); lgc.pack(fill=tk.BOTH, expand=True)
        lbf = tk.Frame(lgc, bg=self.C['card']); lbf.pack(fill=tk.X, pady=(0, 4))
        self._bsm(lbf, '清空日志', lambda: self.log_text.delete('1.0', tk.END) if self.log_text else None, 'text2', 'text2').pack(side=tk.LEFT, padx=1)
        self._bsm(lbf, '复制日志', lambda: None, 'blue', 'blue_hov').pack(side=tk.LEFT, padx=3)
        ltf = tk.Frame(lgc, bg=self.C['border'], padx=1, pady=1); ltf.pack(fill=tk.BOTH, expand=True)
        self.log_text = self.scrolledtext.ScrolledText(
            ltf, font=('Cascadia Mono', 9), relief=tk.FLAT, bd=0,
            wrap=tk.WORD, bg='#1A1E2B', fg='#D4D4D8', insertbackground='white')
        self.log_text.pack(fill=tk.BOTH, expand=True)
        for t, c in [('trigger','#FACC15'),('reply','#4ADE80'),('error','#F87171'),
                     ('info','#60A5FA'),('skip','#9CA3AF'),('poll','#A78BFA')]:
            self.log_text.tag_configure(t, foreground=c)

    # ===== UI 逻辑 =====

    def _append_log(self, msg):
        if not self.log_text: return
        for kw, tag in [('[KEYWORD]','trigger'),('[REPLY]','reply'),('[SEND]','reply'),
                        ('失败','error'),('异常','error'),
                        ('[START]','info'),('[STOP]','info'),('[LOOP]','info'),
                        ('[END]','info'),('[RUN]','info'),
                        ('[SKIP]','skip'),('[POLL]','poll'),('[MATCH]','poll'),
                        ('[DEDUP]','poll'),('[CHAT]','poll'),('[ROUND]','poll'),
                        ('[BASELINE]','poll')]:
            if kw in msg:
                self.log_text.insert(self.tk.END, msg+'\n', tag)
                self.log_text.see(self.tk.END)
                return
        self.log_text.insert(self.tk.END, msg+'\n')
        self.log_text.see(self.tk.END)

    def _refresh_rules_tree(self):
        self.rules_tree.delete(*self.rules_tree.get_children())
        for i, r in enumerate(self.engine.rules):
            status = '[ON]' if r.enabled else '[OFF]'
            self.rules_tree.insert('', self.tk.END, iid=str(i),
                                   values=(status, r.chat, r.keyword, r.reply))

    def _refresh_all(self):
        self._refresh_rules_tree()
        self.mode_var.set(self.engine.mode)
        self.entry_active.delete(0, self.tk.END)
        self.entry_active.insert(0, str(self.engine.active_duration))
        self.entry_pause.delete(0, self.tk.END)
        self.entry_pause.insert(0, str(self.engine.pause_duration))
        self.entry_interval.delete(0, self.tk.END)
        self.entry_interval.insert(0, str(self.engine.poll_interval))
        self._update_hint()
        self._update_loop_params_visibility()

    def _update_loop_params_visibility(self):
        if self.engine.mode == 'loop':
            self.loop_params_frame.pack(side=self.tk.LEFT, padx=20)
        else:
            self.loop_params_frame.pack_forget()

    def _update_start_button(self):
        """根据当前模式只显示对应的启动按钮"""
        # 先都隐藏
        self.btn_start_once.pack_forget()
        self.btn_start_loop.pack_forget()
        self.btn_stop.pack_forget()
        self.led.pack_forget()
        self.round_label.pack_forget()

        # 只显示当前模式的启动按钮
        if self.engine.mode == 'loop':
            self.btn_start_loop.pack(side=self.tk.LEFT, padx=3)
        else:
            self.btn_start_once.pack(side=self.tk.LEFT, padx=3)

        self.btn_stop.pack(side=self.tk.LEFT, padx=3)
        self.led.pack(side=self.tk.LEFT, padx=15)
        self.round_label.pack(side=self.tk.LEFT, padx=10)

    def _on_mode_change(self):
        self.engine.mode = self.mode_var.get()
        self.engine.save_config()
        self._update_loop_params_visibility()
        self._update_start_button()
        self.engine.log('[MODE] 切换至: {}'.format('循环检测' if self.engine.mode=='loop' else '检测一次'))

    def _apply_loop_params(self):
        try:
            a = int(self.entry_active.get().strip())
            if a < 1: raise ValueError
            self.engine.active_duration = a
        except:
            self.messagebox.showwarning('错误', '激活时长必须是 >=1 的整数')
            return
        try:
            p = int(self.entry_pause.get().strip())
            if p < 0: raise ValueError
            self.engine.pause_duration = p
        except:
            self.messagebox.showwarning('错误', '暂停时长必须是 >=0 的整数')
            return
        try:
            v = float(self.entry_interval.get().strip())
            if v < 0: raise ValueError
            self.engine.poll_interval = v
        except:
            self.messagebox.showwarning('错误', '间隔必须是 >=0 的数字')
            return
        self.engine.save_config()
        self.engine.log('[CONFIG] 激活{}秒/暂停{}秒 间隔{}秒'.format(a, p, v))

    def _on_tree_select(self, event):
        sel = self.rules_tree.selection()
        if not sel: return
        idx = int(sel[0])
        if idx >= len(self.engine.rules): return
        r = self.engine.rules[idx]
        self._clear_form()
        self.entry_chat.insert(0, r.chat)
        self.entry_keyword.insert(0, r.keyword)
        self.text_reply.insert('1.0', r.reply)

    def _clear_form(self):
        self.entry_chat.delete(0, self.tk.END)
        self.entry_keyword.delete(0, self.tk.END)
        self.text_reply.delete('1.0', self.tk.END)

    def _add_rule(self):
        c = self.entry_chat.get().strip(); k = self.entry_keyword.get().strip()
        r = self.text_reply.get('1.0', self.tk.END).strip()
        if not c or not k or not r:
            self.messagebox.showwarning('提示', '聊天对象、关键词、回复内容不能为空'); return
        self.engine.add_rule(c, k, r); self._refresh_rules_tree(); self._clear_form(); self._update_hint()

    def _update_rule(self):
        sel = self.rules_tree.selection()
        if not sel:
            self.messagebox.showwarning('提示', '请先在左侧选中一条规则'); return
        idx = int(sel[0])
        if idx >= len(self.engine.rules): return
        c = self.entry_chat.get().strip(); k = self.entry_keyword.get().strip()
        r = self.text_reply.get('1.0', self.tk.END).strip()
        if not c or not k or not r:
            self.messagebox.showwarning('提示', '聊天对象、关键词、回复内容不能为空'); return
        with self.engine._lock:
            self.engine.rules[idx].chat = c; self.engine.rules[idx].keyword = k; self.engine.rules[idx].reply = r
        self.engine.save_config(); self._refresh_rules_tree(); self._clear_form()
        self.engine.log('更新规则#{}: {} | {}'.format(idx+1, c, k))

    def _del_rule(self):
        sel = self.rules_tree.selection()
        if not sel: return
        idx = int(sel[0])
        if idx >= len(self.engine.rules): return
        r = self.engine.rules[idx]
        if self.messagebox.askyesno('确认', '删除规则: {} | {}?'.format(r.chat, r.keyword)):
            self.engine.remove_rule(idx)
            self._refresh_rules_tree()
            self._clear_form()
            self._update_hint()

    def _toggle_rule(self):
        sel = self.rules_tree.selection()
        if not sel: return
        idx = int(sel[0])
        if idx >= len(self.engine.rules): return
        self.engine.toggle_rule(idx)
        self._refresh_rules_tree()

    def _all_toggle(self, enabled):
        for i in range(len(self.engine.rules)):
            self.engine.toggle_rule(i, enabled)
        self._refresh_rules_tree()
        self._update_hint()

    def _start_once(self):
        if self.engine.start(mode='once'):
            self._set_ui_running()

    def _start_loop(self):
        self._apply_loop_params()  # 先应用参数
        if self.engine.start(mode='loop'):
            self._set_ui_running()

    def _stop(self):
        self.engine.stop()
        self._set_ui_stopped()

    def _set_ui_running(self):
        self.btn_start_once.pack_forget(); self.btn_start_loop.pack_forget(); self.btn_stop.pack_forget()
        self.btn_stop.pack(side=self.tk.LEFT, padx=3)
        self.led.pack_forget(); self.led.pack(side=self.tk.LEFT, padx=15)

    def _set_ui_stopped(self):
        self.btn_stop.pack_forget(); self._update_start_button()

    def _save_preset(self):
        name = self.simpledialog.askstring('保存预设', '预设名称:')
        if not name: return
        self.engine.save_preset(name)
        self.messagebox.showinfo('成功', '预设 {} 已保存'.format(name))

    def _load_preset(self):
        tk = self.tk
        presets = self.engine.load_presets()
        if not presets:
            self.messagebox.showinfo('提示', '没有已保存的预设')
            return
        names = sorted(presets.keys())
        dialog = tk.Toplevel(self.root)
        dialog.title('加载预设'); dialog.geometry('400x350')
        dialog.configure(bg='#f0f0f0')
        tk.Label(dialog, text='选择预设:', font=('Microsoft YaHei', 10),
                 bg='#f0f0f0').pack(pady=8)
        lb = tk.Listbox(dialog, font=('Microsoft YaHei', 10), selectmode=tk.SINGLE)
        lb.pack(fill=tk.BOTH, expand=True, padx=10)
        for n in names:
            p = presets[n]
            lb.insert(tk.END, '{} ({}, {}条规则)'.format(
                n, '循环' if p.get('mode')=='loop' else '单次', len(p.get('rules',[]))))
        def do_load():
            sel = lb.curselection()
            if not sel: return
            self.engine.load_preset(names[sel[0]])
            self._refresh_all()
            dialog.destroy()
        btnf = tk.Frame(dialog, bg='#f0f0f0')
        btnf.pack(pady=8)
        tk.Button(btnf, text=' 加载 ', command=do_load, bg='#27ae60', fg='white',
                  font=('Microsoft YaHei', 10), relief=tk.FLAT, padx=15).pack(side=tk.LEFT, padx=5)
        tk.Button(btnf, text=' 取消 ', command=dialog.destroy, bg='#95a5a6', fg='white',
                  font=('Microsoft YaHei', 10), relief=tk.FLAT, padx=15).pack(side=tk.LEFT, padx=5)
        dialog.transient(self.root); dialog.grab_set()
        self.root.wait_window(dialog)

    def _delete_preset(self):
        tk = self.tk
        presets = self.engine.load_presets()
        if not presets:
            self.messagebox.showinfo('提示', '没有已保存的预设')
            return
        names = sorted(presets.keys())
        dialog = tk.Toplevel(self.root)
        dialog.title('删除预设'); dialog.geometry('400x350')
        dialog.configure(bg='#f0f0f0')
        tk.Label(dialog, text='选择要删除的预设:', font=('Microsoft YaHei', 10),
                 bg='#f0f0f0').pack(pady=8)
        lb = tk.Listbox(dialog, font=('Microsoft YaHei', 10))
        lb.pack(fill=tk.BOTH, expand=True, padx=10)
        for n in names: lb.insert(tk.END, n)
        def do_del():
            sel = lb.curselection()
            if not sel: return
            n = names[sel[0]]
            if self.messagebox.askyesno('确认', '删除预设 {}?'.format(n), parent=dialog):
                self.engine.delete_preset(n)
                dialog.destroy()
        btnf = tk.Frame(dialog, bg='#f0f0f0')
        btnf.pack(pady=8)
        tk.Button(btnf, text=' 删除 ', command=do_del, bg='#e74c3c', fg='white',
                  font=('Microsoft YaHei', 10), relief=tk.FLAT, padx=15).pack(side=tk.LEFT, padx=5)
        tk.Button(btnf, text=' 取消 ', command=dialog.destroy, bg='#95a5a6', fg='white',
                  font=('Microsoft YaHei', 10), relief=tk.FLAT, padx=15).pack(side=tk.LEFT, padx=5)
        dialog.transient(self.root); dialog.grab_set()
        self.root.wait_window(dialog)

    def _update_status(self):
        st = self.engine.status; labels = self.status_labels
        running = self.engine.monitor_running
        labels['monitor'].config(text='运行中' if running else '已停止',
                                 fg=self.C['primary'] if running else self.C['danger'])
        self.header_badge.config(text='运行中' if running else '已停止',
                                 bg=self.C['primary'] if running else self.C['danger'])
        self.led.config(text='O 运行中' if running else 'O 已停止',
                        fg=self.C['primary'] if running else self.C['danger'])
        labels['mode'].config(text=st.get('mode','-'))
        labels['current_chat'].config(text=(st.get('current_chat','-') or '-')[:25])
        labels['last_message'].config(text=(st.get('last_message','-') or '-')[:30])
        labels['last_trigger'].config(text=(st.get('last_trigger','-') or '-')[:30])
        labels['last_send'].config(text=(st.get('last_send','-') or '-')[:20])
        rn = st.get('round',0); total = st.get('total_rounds',0)
        if running and total == '-': self.round_label.config(text='第 {} 轮'.format(rn))
        elif running: self.round_label.config(text='第 {}/{} 轮'.format(rn, total))
        else: self.round_label.config(text='')
        if not running: self._set_ui_stopped()
        self._update_hint()
        self.root.after(1000, self._update_status)

    def _update_hint(self):
        enabled = sum(1 for r in self.engine.rules if r.enabled)
        running = self.engine.monitor_running
        if enabled and not running:
            self.hint_label.config(text='已配置 {} 条规则，点击启动按钮开始'.format(enabled))
        elif not enabled:
            self.hint_label.config(text='还没有添加规则，请先在右侧填写并添加')
        elif running:
            m = '循环' if self.engine.mode=='loop' else '单次'
            self.hint_label.config(text='{} 检测中，{} 条规则启用'.format(m, enabled))
        else: self.hint_label.config(text='')

    def _pull_logs(self):
        if not self.log_text or not self.root: return
        try:
            while True: self._append_log(self.engine.log_queue.get_nowait())
        except queue.Empty: pass
        self.root.after(300, self._pull_logs)

    def _console_mode(self):
        print('='*55)
        print('  wxauto 自动回复监控 v4 - 控制台模式')
        print('='*55)
        self.engine.load_config()
        print('模式: {} | 规则: {}条'.format(
            '循环' if self.engine.mode=='loop' else '单次', len(self.engine.rules)))
        for i, r in enumerate(self.engine.rules):
            print('  #{}. {} {} -> {}'.format(i+1, r.chat, r.keyword, r.reply[:30]))
        print()
        print('命令: once | loop | start | stop | quit | add:聊天|关键词|回复')
        print('      del:# | toggle:# | preset:save:name | preset:load:name')
        print('      按 Ctrl+C 可强制停止')
        # Ctrl+C 信号处理
        def _sigint(sig, frame):
            self.engine.log('[CTRL+C] 强制中断')
            self.engine.stop()
        try:
            signal.signal(signal.SIGINT, _sigint)
        except: pass
        def log_printer():
            while self.engine.monitor_running or not self.engine.log_queue.empty():
                try: print(self.engine.log_queue.get(timeout=0.5))
                except queue.Empty:
                    if not self.engine.monitor_running: break
        while True:
            try: cmd = input('>> ').strip()
            except (EOFError, KeyboardInterrupt):
                self.engine.stop_requested = True
                self.engine.monitor_running = False
                self.engine.stop()
                break
            if not cmd: continue
            if cmd == 'quit': self.engine.stop(); break
            if cmd == 'once': self.engine.start('once'); continue
            if cmd == 'loop':
                self.engine.start('loop')
                threading.Thread(target=log_printer, daemon=True).start()
                continue
            if cmd == 'start':
                self.engine.start()
                threading.Thread(target=log_printer, daemon=True).start()
                continue
            if cmd == 'stop': self.engine.stop(); continue
            if cmd.startswith('add:'):
                parts = cmd[4:].split('|',2)
                if len(parts)==3:
                    self.engine.add_rule(parts[0].strip(), parts[1].strip(), parts[2].strip())
                continue
            if cmd.startswith('del:'):
                try: self.engine.remove_rule(int(cmd[4:])-1)
                except: pass
                continue
            if cmd.startswith('toggle:'):
                try: self.engine.toggle_rule(int(cmd[7:])-1)
                except: pass
                continue
            if cmd.startswith('preset:save:'):
                self.engine.save_preset(cmd[12:].strip()); continue
            if cmd.startswith('preset:load:'):
                self.engine.load_preset(cmd[12:].strip()); self._refresh_all()
                continue
        print('已退出')


def main():
    engine = MonitorEngine()
    if '--console' in sys.argv:
        AutoReplyUI(engine)._console_mode()
    else:
        AutoReplyUI(engine).build_and_run()

if __name__ == '__main__':
    main()
