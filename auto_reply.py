"""
wxauto 4.1 自动回复监控系统 v4
--------------------------------
模式: 检测一次 / 循环检测
多规则: 每规则独立 (聊天+关键词+回复)
预设: 保存/加载/删除 配置快照
"""
import sys, os, json, time, threading, queue, hashlib, copy, ctypes, signal
from collections import deque
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
VK_C     = 0x43
WM_HOTKEY = 0x0312

class GlobalHotkey:
    """Win32 全局热键 — 创建隐藏窗口, 在主线程消息循环中处理"""

    _hwnd = None
    _callbacks = {}
    _next_id = 0

    @classmethod
    def _ensure_window(cls):
        if cls._hwnd: return
        # 注册窗口类
        wc_name = 'WxAutoHotkeyWnd'
        wndproc = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p,
                                      ctypes.c_uint, ctypes.c_ulonglong, ctypes.c_longlong)
        def _proc(hwnd, msg, wparam, lparam):
            if msg == WM_HOTKEY:
                cb = cls._callbacks.get(wparam)
                if cb:
                    threading.Thread(target=cb, daemon=True).start()
                return 0
            return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        cls._wndproc = wndproc(_proc)

        wc = wintypes.WNDCLASSW()
        wc.lpfnWndProc = cls._wndproc
        wc.lpszClassName = wc_name
        wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        ctypes.windll.user32.RegisterClassW(ctypes.byref(wc))

        cls._hwnd = ctypes.windll.user32.CreateWindowExW(
            0, wc_name, '', 0, 0, 0, 0, 0, None, None, wc.hInstance, None)

    @classmethod
    def register(cls, modifiers, vk, callback):
        cls._ensure_window()
        cls._next_id += 1
        hkid = cls._next_id
        cls._callbacks[hkid] = callback
        ok = ctypes.windll.user32.RegisterHotKey(cls._hwnd, hkid, modifiers, vk)
        if not ok:
            raise RuntimeError('RegisterHotKey failed (err={})'.format(
                ctypes.windll.kernel32.GetLastError()))
        return hkid

    @classmethod
    def unregister(cls, hkid):
        if cls._hwnd:
            ctypes.windll.user32.UnregisterHotKey(cls._hwnd, hkid)
        cls._callbacks.pop(hkid, None)

    @classmethod
    def pump_once(cls):
        """在主线程调用一次, 处理消息队列中的WM_HOTKEY"""
        if not cls._hwnd: return
        msg = wintypes.MSG()
        while ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), cls._hwnd, 0, 0, 1):
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))


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


def app_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# ==================== UIA 导航 ====================

class FreshNav:
    """每次从 HWND 全新建立 UIA 树, 杜绝stale引用"""

    @staticmethod
    def _try_uia(max_retries=3):
        for i in range(max_retries):
            try:
                h = get_hwnd()
                if not h:
                    h = win32gui.FindWindow('mmui::MainWindow', None)
                if not h: return None
                init_com()
                return uia.ControlFromHandle(h)
            except:
                if i == max_retries - 1: return None
                time.sleep(0.3)
        return None

    @staticmethod
    def _nav_outer_splitter():
        """导航到外层 XSplitterView (含左侧会话列表+右侧聊天面板)"""
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

    @staticmethod
    def _nav_right_panel():
        """导航到右侧聊天面板 XStackedWidget"""
        outer = FreshNav._nav_outer_splitter()
        if not outer: return None
        try:
            children = outer.GetChildren()
            rights = [c for c in children if c.ClassName == 'mmui::XStackedWidget']
            return rights[0] if rights else None
        except: return None

    @staticmethod
    def _get_chat_page(st):
        """从右侧 XStackedWidget 获取 ChatMessagePage"""
        if not st: return None
        try:
            for c in st.GetChildren():
                if c.ClassName == 'mmui::ChatMessagePage':
                    return c
        except: pass
        return None

    @staticmethod
    def get_message_items():
        """获取当前聊天消息。返回 (chat_name, [items])"""
        st = FreshNav._nav_right_panel()
        if not st: return None, []
        cp = FreshNav._get_chat_page(st)
        if cp is None: return None, []
        # 聊天名
        tb = cp.GetChildren()[0]
        def find_name(ele):
            try:
                if ele.ControlTypeName=='TextControl' and ele.ClassName=='mmui::XTextView':
                    n = ele.Name or ''
                    if n and not (n.startswith('(') and n.endswith(')')): return n
                for c in ele.GetChildren():
                    r = find_name(c); 
                    if r: return r
            except: pass
            return None
        chat_name = find_name(tb)
        # 消息列表 (4.1.10: XVBoxView包裹MessageView, 需要递归找)
        split = cp.CustomControl(ClassName='mmui::XSplitterView')
        # 递归找 MessageView
        def _find_msg_view(ele):
            try:
                if ele.ClassName == 'mmui::MessageView':
                    return ele
                for c in ele.GetChildren():
                    r = _find_msg_view(c)
                    if r: return r
            except: pass
            return None
        mv = _find_msg_view(split)
        if not mv: return chat_name, []
        lst = mv.ListControl(ClassName='mmui::RecyclerListView')
        return chat_name, lst.GetChildren()

    @staticmethod
    def get_session_table():
        """获取左侧会话列表"""
        outer = FreshNav._nav_outer_splitter()
        if not outer: return None
        try:
            children = outer.GetChildren()
            lefts = [c for c in children if c.ClassName=='mmui::XView']
            if not lefts: return None
            cmv = lefts[0].GroupControl(ClassName='mmui::ChatMasterView')
            cl = cmv.GroupControl(ClassName='mmui::XView')
            ar = [c for c in cl.GetChildren() if c.ClassName=='mmui::XView']
            if len(ar)<2: return None
            li = ar[1].GroupControl(ClassName='mmui::XView')
            sl = li.GroupControl(ClassName='mmui::ChatSessionList')
            return sl.ListControl(ClassName='mmui::XTableView')
        except: return None

    @staticmethod
    def get_search_edit():
        """返回搜索框"""
        outer = FreshNav._nav_outer_splitter()
        if not outer: return None
        try:
            children = outer.GetChildren()
            lefts = [c for c in children if c.ClassName=='mmui::XView']
            if not lefts: return None
            cmv = lefts[0].GroupControl(ClassName='mmui::ChatMasterView')
            cl = cmv.GroupControl(ClassName='mmui::XView')
            sf = cl.GroupControl(ClassName='mmui::XSearchField')
            return sf.EditControl(ClassName='mmui::XValidatorTextEdit')
        except: return None

    @staticmethod
    def get_input_field():
        """返回输入框 EditControl"""
        st = FreshNav._nav_right_panel()
        if not st: return None
        cp = FreshNav._get_chat_page(st)
        if cp is None: return None
        try:
            split = cp.CustomControl(ClassName='mmui::XSplitterView')
            xvs = [c for c in split.GetChildren() if c.ClassName=='mmui::XView']
            if not xvs: return None
            ig = xvs[0].GroupControl(ClassName='mmui::InputView')
            xv2 = ig.GroupControl(ClassName='mmui::XView')
            ixv = xv2.GroupControl(ClassName='mmui::XView')
            return ixv.EditControl(ClassName='mmui::ChatInputField')
        except: return None

    @staticmethod
    def get_input_rect():
        """输入框坐标"""
        st = FreshNav._nav_right_panel()
        if not st: return None
        cp = FreshNav._get_chat_page(st)
        if cp is None: return None
        try:
            split = cp.CustomControl(ClassName='mmui::XSplitterView')
            xvs = [c for c in split.GetChildren() if c.ClassName=='mmui::XView']
            if not xvs: return None
            r = xvs[0].BoundingRectangle
            return (r.left, r.top, r.right, r.bottom)
        except: return None

    @staticmethod
    def switch_to_chat_tab():
        win = FreshNav._try_uia()
        if not win: return
        root = win.GroupControl(searchDepth=1)
        stacked = root.CustomControl(ClassName='QStackedWidget')
        mv = stacked.GroupControl(ClassName='mmui::MainView')
        nav = mv.ToolBarControl(ClassName='mmui::MainTabBar')
        nav.ButtonControl(Name='微信').Click(simulateMove=False)


# ==================== 数据模型 ====================

class Rule:
    __slots__ = ('chat','keyword','reply','enabled')
    def __init__(self, chat='', keyword='', reply='', enabled=True):
        self.chat = chat; self.keyword = keyword; self.reply = reply; self.enabled = enabled
    def to_dict(self):
        return {'chat':self.chat,'keyword':self.keyword,'reply':self.reply,'enabled':self.enabled}
    @staticmethod
    def from_dict(d):
        return Rule(d.get('chat',''), d.get('keyword',''), d.get('reply',''), d.get('enabled',True))

class RawMsg:
    __slots__ = ('cls','name','runtime_id')
    def __init__(self, item):
        self.cls = item.ClassName; self.name = item.Name or ''
        try: self.runtime_id = ''.join(str(i) for i in item.GetRuntimeId())
        except: self.runtime_id = ''
    @property
    def is_timestamp(self): return self.cls == 'mmui::ChatItemView'
    @property
    def is_text(self): return self.cls == 'mmui::ChatTextItemView'
    @property
    def content_hash(self):
        return hashlib.md5(self.name.encode('utf-8','replace')).hexdigest()[:16]


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
        self._record_lock = threading.Lock()
        self._hotkey_thread = None
        self._hotkey_stop_event = threading.Event()
        self._hotkey_latched = False
        self.log_queue = queue.Queue()
        self.reply_record_queue = queue.Queue()

        self.baseline_ids = {}
        self.last_incoming = {}   # {chat: text} 上次已回复的消息
        self.pinned_chats = set()   # 已浮到顶部的聊天, 下次不用搜
        self.prepared_chats = set()   # 启动时已预热到会话列表前方的聊天
        self.sent_texts = set()   # 自己发过的文本(防循环)
        self.sent_text_order = []
        self.stop_requested = False  # 用于 Ctrl+C / 超时 / 手动停止
        self._rules_version = 0
        self._rule_cache_dirty = True
        self._rule_groups_cache = {}
        self._enabled_rules_cache = 0
        self._records_version = 0

        self.status = {
            'mode': '检测一次', 'current_chat': '', 'last_message': '',
            'last_trigger': '', 'last_send': '', 'round': 0, 'total_rounds': 0,
            'sent_ok': 0, 'sent_fail': 0, 'last_record_time': '',
        }

        base_dir = app_base_dir()
        self.config_path = os.path.join(base_dir, 'auto_reply_config.json')
        self.presets_path = os.path.join(base_dir, 'auto_reply_presets.json')
        self.reply_records_path = os.path.join(base_dir, 'auto_reply_records.jsonl')

    def log(self, msg):
        t = datetime.now().strftime('%H:%M:%S')
        text = '[{}] {}'.format(t, msg)
        self.log_queue.put(text)
        try: print(text)
        except: print(text.encode('ascii','replace').decode('ascii','replace'))

    @property
    def records_version(self):
        return self._records_version

    def _mark_rules_dirty(self):
        self._rules_version += 1
        self._rule_cache_dirty = True

    def _get_rule_groups(self):
        """返回按聊天分组后的启用规则快照，避免每轮重复归一化关键词。"""
        with self._lock:
            if self._rule_cache_dirty:
                grouped = {}
                enabled = 0
                for idx, rule in enumerate(self.rules):
                    if not rule.enabled:
                        continue
                    keyword_norm = self._normalize(rule.keyword)
                    if not rule.chat.strip() or not keyword_norm or not rule.reply.strip():
                        continue
                    enabled += 1
                    grouped.setdefault(rule.chat.strip(), []).append((idx, rule, keyword_norm))
                self._rule_groups_cache = grouped
                self._enabled_rules_cache = enabled
                self._rule_cache_dirty = False
            return ({chat: list(items) for chat, items in self._rule_groups_cache.items()},
                    self._enabled_rules_cache, self._rules_version)

    def enabled_rule_count(self):
        _, enabled, _ = self._get_rule_groups()
        return enabled

    def _sleep_interruptible(self, seconds, step=0.05):
        end_at = time.time() + max(float(seconds), 0)
        while not self.stop_requested:
            left = end_at - time.time()
            if left <= 0:
                break
            time.sleep(min(step, left))

    def _start_hotkey_watcher(self):
        if self._hotkey_thread and self._hotkey_thread.is_alive():
            self._hotkey_stop_event.clear()
            return
        self._hotkey_stop_event.clear()
        self._hotkey_latched = False
        self._hotkey_thread = threading.Thread(target=self._hotkey_watch_loop, daemon=True)
        self._hotkey_thread.start()
        self.log('[HOTKEY] Ctrl+Shift+C 全局检测线程已启动')

    def _hotkey_watch_loop(self):
        """轮询全局按键状态，避免依赖当前窗口焦点或 Tk 消息循环。"""
        while not self._hotkey_stop_event.is_set():
            try:
                ctrl_down = bool(win32api.GetAsyncKeyState(win32con.VK_CONTROL) & 0x8000)
                shift_down = bool(win32api.GetAsyncKeyState(win32con.VK_SHIFT) & 0x8000)
                c_down = bool(win32api.GetAsyncKeyState(VK_C) & 0x8000)
                pressed = ctrl_down and shift_down and c_down
                if pressed and not self._hotkey_latched:
                    self._hotkey_latched = True
                    if self.monitor_running:
                        self.log('[HOTKEY] Ctrl+Shift+C 已触发，强制退出循环')
                        self.stop()
                elif not pressed:
                    self._hotkey_latched = False
            except Exception as e:
                self.log('[HOTKEY] 全局检测异常: {}'.format(str(e)[:60]))
                break
            time.sleep(0.05)

    def _remember_sent(self, text):
        text = (text or '').strip()
        if not text:
            return
        if text not in self.sent_texts:
            self.sent_text_order.append(text)
        self.sent_texts.add(text)
        while len(self.sent_text_order) > 200:
            old = self.sent_text_order.pop(0)
            self.sent_texts.discard(old)

    def _record_reply(self, rule_index, rule, chat, sender, incoming, success):
        record = {
            'time': datetime.now().isoformat(timespec='seconds'),
            'mode': self.mode,
            'chat': chat,
            'sender': sender,
            'rule_index': rule_index + 1,
            'keyword': rule.keyword,
            'reply': rule.reply,
            'incoming_message': incoming,
            'success': bool(success),
        }
        try:
            with self._record_lock:
                with open(self.reply_records_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(record, ensure_ascii=False) + '\n')
                self._records_version += 1
        except Exception as e:
            self.log('[RECORD] 保存回复记录失败: {}'.format(str(e)[:60]))
        else:
            self.reply_record_queue.put(record)
            if success:
                self.status['sent_ok'] = self.status.get('sent_ok', 0) + 1
            else:
                self.status['sent_fail'] = self.status.get('sent_fail', 0) + 1
            self.status['last_record_time'] = record['time'][11:19]

    def load_reply_records(self, limit=200):
        rows = deque(maxlen=max(int(limit), 1))
        if not os.path.exists(self.reply_records_path):
            return []
        try:
            with open(self.reply_records_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            self.log('[RECORD] 读取回复记录失败: {}'.format(str(e)[:60]))
        return list(rows)

    def clear_reply_records(self):
        try:
            with self._record_lock:
                with open(self.reply_records_path, 'w', encoding='utf-8'):
                    pass
                self._records_version += 1
            self.log('[RECORD] 回复记录已清空')
            return True
        except Exception as e:
            self.log('[RECORD] 清空回复记录失败: {}'.format(str(e)[:60]))
            return False

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
                self._mark_rules_dirty()
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
        self._mark_rules_dirty()
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
            self._mark_rules_dirty()
            self.save_config()
            self.log('添加规则: {} | {} | {}...'.format(chat, keyword, reply[:25]))

    def remove_rule(self, index):
        with self._lock:
            if 0 <= index < len(self.rules):
                r = self.rules.pop(index)
                self._mark_rules_dirty()
                self.save_config()
                self.log('删除规则: {} | {}'.format(r.chat, r.keyword))

    def toggle_rule(self, index, enabled=None):
        with self._lock:
            if 0 <= index < len(self.rules):
                if enabled is None:
                    self.rules[index].enabled = not self.rules[index].enabled
                else:
                    self.rules[index].enabled = enabled
                self._mark_rules_dirty()
                self.save_config()

    def update_rule(self, index, chat, keyword, reply):
        with self._lock:
            if not (0 <= index < len(self.rules)):
                return False
            self.rules[index].chat = chat
            self.rules[index].keyword = keyword
            self.rules[index].reply = reply
            self._mark_rules_dirty()
            self.save_config()
        self.log('更新规则#{}: {} | {}'.format(index+1, chat, keyword))
        return True

    def set_all_rules(self, enabled):
        with self._lock:
            for rule in self.rules:
                rule.enabled = enabled
            self._mark_rules_dirty()
            self.save_config()
        self.log('[CONFIG] 已{}全部规则'.format('启用' if enabled else '禁用'))

    # ---- 核心操作 ----

    def _on_target(self, who):
        """检查当前是否已在目标聊天(纯读UIA, 不切不点击)"""
        try:
            cur_name, _ = FreshNav.get_message_items()
            return cur_name and cur_name.strip() == who.strip()
        except: return False

    def _open_chat(self, who):
        """打开指定会话: 当前页→会话列表点击→搜索框"""
        try:
            if self._on_target(who):
                self.status["current_chat"] = who
                self.pinned_chats.add(who)
                self.prepared_chats.add(who)
                return True
            if who in self.prepared_chats or who in self.pinned_chats:
                if self._click_visible_cell(who):
                    self._sleep_interruptible(0.08)
                    if self._on_target(who):
                        self.pinned_chats.add(who)
                        self.prepared_chats.add(who)
                        return True
                    self.log('[CHAT] 会话列表点击后验证失败, 回退搜索: {}'.format(who[:20]))
            return self.__open_chat(who)
        except (LookupError, Exception) as e:
            self.log('[ERR] _open_chat失败: {}'.format(str(e)[:50]))
            return False

    @staticmethod
    def _session_cell_title(cell):
        try:
            return (cell.Name or '').split(chr(10))[0].strip()
        except Exception:
            return ''

    def _click_visible_cell(self, who):
        """在可见的 ChatSessionCell 中查找并点击目标聊天"""
        cells = []
        try:
            table = FreshNav.get_session_table()
            if table:
                cells.extend(table.GetChildren())
        except: pass

        if not cells:
            win = FreshNav._try_uia()
            if not win: return False
            def rec(ele):
                try:
                    if (ele.ControlTypeName == 'ListItemControl' and
                        ele.ClassName and 'ChatSessionCell' in ele.ClassName):
                        cells.append(ele)
                except: pass
                try:
                    for cc in ele.GetChildren():
                        rec(cc)
                except: pass
            rec(win)

        target = who.strip()
        for c in cells:
            try:
                cls = c.ClassName or ''
                if 'ChatSessionCell' not in cls and c.ControlTypeName != 'ListItemControl':
                    continue
                if self._session_cell_title(c) == target:
                    r = c.BoundingRectangle
                    mouse_click((r.left+r.right)//2, (r.top+r.bottom)//2)
                    self._sleep_interruptible(0.12)
                    self.status["current_chat"] = who
                    self.log('[CHAT] 已从会话列表切换: {}'.format(who[:20]))
                    return True
            except: pass
        return False

    def __open_chat(self, who):
        """打开聊天: 已在目标→不切; 首次→Ctrl+F→验证; 最多重试3次"""
        init_com()

        # 已在目标 → 不操作
        try:
            cur_name, _ = FreshNav.get_message_items()
            if cur_name and cur_name.strip() == who.strip():
                self.status["current_chat"] = who
                self.pinned_chats.add(who)
                self.prepared_chats.add(who)
                return True
        except: pass

        for attempt in range(3):
            if attempt > 0:
                self.log('[RETRY] 第{}次重试打开: {}'.format(attempt+1, who[:12]))
                self._sleep_interruptible(0.5)

            if self.stop_requested:
                return False
            focus_wechat(); self._sleep_interruptible(0.08)

            # Ctrl+F 搜索 + Enter 打开
            uia.SendKeys("{Ctrl}f", waitTime=0.1)
            self._sleep_interruptible(0.3)
            pyperclip.copy(who)
            uia.SendKeys("{Ctrl}v", waitTime=0.05)
            self._sleep_interruptible(0.35)
            uia.SendKeys("{Enter}", waitTime=0.05)
            self._sleep_interruptible(0.8)

            # 验证是否真的打开了
            try:
                cur_name, _ = FreshNav.get_message_items()
                if cur_name and cur_name.strip() == who.strip():
                    self.pinned_chats.add(who)
                    self.prepared_chats.add(who)
                    self.status["current_chat"] = who
                    return True
            except: pass

        self.log('[FAIL] 3次重试均无法打开: {}'.format(who[:20]))
        return False
    def _send_reply(self, text):
        # 抢焦点到微信 (后台线程可能无法SetForegroundWindow, 用try保护)
        try:
            focus_wechat()
            time.sleep(0.15)
        except Exception:
            pass  # 即使抢不到焦点, 也尝试发送
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
                    self._remember_sent(text)
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

        self._remember_sent(text)
        self.log('[SEND] 成功')
        return True

    def _is_self(self, text):
        t = text.strip()
        return t in self.sent_texts

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
        return k in m

    def _prepare_rule_chats(self, grouped):
        """多会话启动预热: 先逐个打开规则会话，使其进入会话列表前方。"""
        chats = [chat for chat in grouped.keys() if chat.strip()]
        if len(chats) <= 1:
            return

        self.log('[PREPARE] 检测到 {} 个规则会话, 开始预热会话列表'.format(len(chats)))
        ok_count = 0
        for i, chat in enumerate(chats, 1):
            if self.stop_requested:
                break
            self.status['current_chat'] = '预热 {}/{} {}'.format(i, len(chats), chat[:12])
            self.log('[PREPARE] ({}/{}) 打开会话: {}'.format(i, len(chats), chat[:20]))
            if self.__open_chat(chat):
                ok_count += 1
                self.prepared_chats.add(chat)
                self.pinned_chats.add(chat)
                self._sleep_interruptible(0.12)
            else:
                self.log('[PREPARE] 会话预热失败: {}'.format(chat[:20]))

        self.log('[PREPARE] 会话预热完成: {}/{}，后续优先从会话列表点击切换'.format(
            ok_count, len(chats)))

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
        self.pinned_chats.clear()  # 重新开始时清空
        self.prepared_chats.clear()
        self.sent_texts.clear()
        self.sent_text_order.clear()
        self.stop_requested = False
        self.status = {
            'mode': '循环检测' if self.mode=='loop' else '检测一次',
            'current_chat': '', 'last_message': '',
            'last_trigger': '', 'last_send': '',
            'round': 0, 'total_rounds': 0,
            'sent_ok': 0, 'sent_fail': 0, 'last_record_time': '',
        }
        self.monitor_running = True
        self._start_hotkey_watcher()
        self._thread = threading.Thread(target=self._runner, daemon=True)
        self._thread.start()

        # 注册全局热键 Ctrl+Shift+C 强制停止(只注册一次)
        if not hasattr(MonitorEngine, '_hotkey_hkid'):
            try:
                _stop_self = self.stop
                MonitorEngine._hotkey_hkid = GlobalHotkey.register(
                    MOD_CONTROL | MOD_SHIFT, VK_C,
                    lambda: _stop_self())
                self.log('[HOTKEY] Ctrl+Shift+C 全局强制停止已注册')
            except Exception as e:
                MonitorEngine._hotkey_hkid = None
                if '1409' not in str(e) and '1410' not in str(e):
                    self.log('[HOTKEY] 注册失败: {}'.format(e))

        # 控制台 Ctrl+C 信号处理
        try:
            signal.signal(signal.SIGINT, lambda sig, frame: self.stop())
        except: pass

        if self.mode == 'loop':
            self.log('[START] 模式: 循环 | 激活{}秒/暂停{}秒 | 间隔{}秒 | 规则: {}条'.format(
                self.active_duration, self.pause_duration, self.poll_interval,
                self.enabled_rule_count()))
        else:
            self.log('[START] 模式: 单次 | 规则: {}条'.format(
                self.enabled_rule_count()))
        return True

    def stop(self):
        self.stop_requested = True
        self._hotkey_stop_event.set()
        if not self.monitor_running: return
        self.monitor_running = False
        if self._thread and threading.current_thread() is not self._thread:
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
        groups, enabled, _ = self._get_rule_groups()
        if enabled:
            self._prepare_rule_chats(groups)

        if self.stop_requested:
            pass
        elif self.mode == 'once':
            self._poll_once()
        else:
            self._loop_forever(t0)

        self.monitor_running = False
        self._hotkey_stop_event.set()
        elapsed = int(time.time() - t0)
        self.log('[END] 运行结束，耗时 {} 秒，共 {} 轮'.format(elapsed, self.status['round']))

    def _poll_once(self):
        """循环检测直到匹配并回复一次, 然后停止"""
        _, enabled, _ = self._get_rule_groups()
        if not enabled:
            self.log('[SKIP] 没有启用的规则')
            return
        self.log('[ONCE] 等待匹配消息, 检测到后回复一次并停止')
        self.status['total_rounds'] = 1
        while not self.stop_requested:
            self.status['round'] += 1
            try:
                groups, enabled, rules_version = self._get_rule_groups()
                if not enabled:
                    self.log('[SKIP] 没有启用的规则')
                    break
                if self._poll_all_rules(groups, is_once=True, rules_version=rules_version):
                    self.log('[ONCE] 已回复, 停止检测')
                    break
            except Exception as e:
                self.log('[ERR] 本轮异常: {}'.format(str(e)[:60]))
            self._sleep_interruptible(self.poll_interval)

    def _loop_forever(self, start_time):
        """循环检测: 激活N秒 → 暂停N秒 → 重复，直到手动停止"""
        active_dur = self.active_duration
        pause_dur = self.pause_duration
        self.log('[LOOP] 激活{}秒 / 暂停{}秒 循环中...'.format(active_dur, pause_dur))

        cycle = 0
        while not self.stop_requested:
            cycle += 1

            # ==== 激活阶段 ====
            phase_start = time.time()
            self.log('[LOOP] 周期#{} 激活开始(最长{}秒)'.format(cycle, active_dur))

            while not self.stop_requested:
                elapsed = time.time() - phase_start
                if elapsed >= active_dur: break

                groups, enabled, rules_version = self._get_rule_groups()
                if not enabled:
                    self._sleep_interruptible(0.3)
                    continue

                try:
                    self.status['round'] += 1
                    self.status['total_rounds'] = '-'
                    self._poll_all_rules(groups, is_once=False, rules_version=rules_version)
                    self._sleep_interruptible(max(self.poll_interval, 0.1))
                except (LookupError, Exception) as e:
                    self.log('[ERR] 本轮异常: {}'.format(str(e)[:60]))
                    self._sleep_interruptible(1)

            # ==== 暂停阶段 ====
            if pause_dur > 0 and not self.stop_requested:
                self.log('[LOOP] 周期#{} 暂停{}秒'.format(cycle, pause_dur))
                self._sleep_interruptible(pause_dur, step=0.2)

    def _poll_all_rules(self, grouped, is_once, rules_version):
        """按聊天分组检测"""
        single = (len(grouped) == 1)

        for chat, rule_list in grouped.items():
            if self.stop_requested: break

            # 1. 确保在目标聊天
            need_open = True
            cur_name, items = None, []
            if single and chat in self.pinned_chats:
                # 单聊天已定位时直接读当前页面，避免每轮重复搜索和二次 UIA 读取。
                cur_name, items = FreshNav.get_message_items()
                if cur_name and cur_name.strip() == chat.strip():
                    need_open = False
                else:
                    self.pinned_chats.discard(chat)
            if need_open:
                if not self._open_chat(chat):
                    self.log('[SKIP] 规则#{} 无法打开: {}'.format(rule_list[0][0]+1, chat))
                    continue
                self._sleep_interruptible(0.05)
                cur_name, items = FreshNav.get_message_items()

            # 2. 取最后一条 incoming 消息
            if not cur_name or cur_name.strip() != chat.strip():
                self.log('[SKIP] 聊天不匹配: 期望[{}] 实际[{}]'.format(chat, cur_name))
                continue

            latest = None
            try:
                item_iter = reversed(items)
            except TypeError:
                item_iter = reversed(list(items))
            for item in item_iter:
                msg = RawMsg(item)
                if msg.is_timestamp or not msg.name.strip():
                    continue
                latest = msg
                break
            if latest is None:
                continue

            full = latest.name
            sender = ''
            content = full
            if '\n' in full:
                head, body = full.split('\n', 1)
                if len(head) < 30:
                    sender = head
                    content = body

            is_self = self._is_self(full) or self._is_self(content)
            self.status['current_chat'] = chat
            self.status['last_message'] = content[:60]

            # 3. 去重: 和上次相同则跳过
            msg_key = latest.runtime_id or latest.content_hash
            dedup_key = '{}|{}|{}|{}'.format(rules_version, msg_key, sender, content.strip())
            if dedup_key == self.last_incoming.get(chat, ''):
                continue
            self.last_incoming[chat] = dedup_key

            # 4. 不回复自己
            if is_self:
                continue

            # 5. 逐条规则匹配
            normalized_content = self._normalize(content)
            for idx, rule, keyword_norm in rule_list:
                if keyword_norm not in normalized_content:
                    continue
                if rule.reply.strip() in full:
                    continue

                self.log('[KEYWORD] 触发! [{}] kw={!r}'.format(chat, rule.keyword))
                ok = self._send_reply(rule.reply)
                self._record_reply(idx, rule, chat, sender, content, ok)
                if ok:
                    self.log('[REPLY] -> [{}]: {}'.format(chat, rule.reply[:40]))
                    self.status['last_trigger'] = '{} | {}'.format(chat, rule.keyword)
                    self.status['last_send'] = '成功'
                else:
                    self.status['last_send'] = '失败'
                return ok  # 每条消息只尝试回复一次；单次模式仅成功后结束
        return False


# ==================== Tkinter UI ====================

class AutoReplyUI:

    C = {'bg':'#F3F6FB','card':'#FFFFFF','header':'#173B3F',
         'header_sub':'#BFE7E3','primary':'#10B981','primary_hov':'#059669',
         'blue':'#2563EB','blue_hov':'#3B82F6','cyan':'#0891B2',
         'danger':'#EF4444','danger_hov':'#F87171','warning':'#F59E0B',
         'purple':'#7C3AED','pink':'#DB2777','text':'#172033',
         'text2':'#667085','border':'#D8E0EA','row_even':'#F8FAFC',
         'row_hover':'#DFF7EA','soft_green':'#ECFDF5','soft_blue':'#EFF6FF',
         'soft_yellow':'#FFFBEB','soft_red':'#FEF2F2',}

    def __init__(self, engine):
        self.engine = engine
        engine.load_config()
        self.root = None
        self.log_text = None
        self.records_tree = None
        self.header_rules = None
        self._records_seen_version = -1
        self._pulse_on = False

    def _btn(self, parent, text, cmd, color, hcolor, w=9, fs=10):
        tk = self.tk; c=self.C.get(color,color); hc=self.C.get(hcolor,hcolor)
        b = tk.Frame(parent, bg=self.C['border'], cursor='hand2', padx=1, pady=1)
        i = tk.Frame(b, bg=c); i.pack(fill=tk.BOTH, expand=True)
        l = tk.Label(i, text=text, bg=c, fg='white',
                     font=('Microsoft YaHei UI', fs, 'bold'), padx=w, pady=3)
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
            tk.Frame(h, bg=self.C['primary'], width=4).pack(side=tk.LEFT, fill=tk.Y, padx=(0, 7))
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
        self.root.geometry('1400x1000')
        self.root.minsize(900, 650)
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
                 fg=self.C['header_sub'], font=('Microsoft YaHei UI', 9)).pack(side=tk.LEFT, padx=12)
        self.header_badge = tk.Label(hdr, text='已停止', bg=self.C['danger'], fg='white',
                                     font=('Microsoft YaHei UI', 9, 'bold'), padx=10, pady=2)
        self.header_badge.pack(side=tk.RIGHT)
        self.header_rules = tk.Label(hdr, text='0 条启用规则', bg='#245D63', fg='white',
                                     font=('Microsoft YaHei UI', 9, 'bold'), padx=10, pady=2)
        self.header_rules.pack(side=tk.RIGHT, padx=8)

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
                                          height=10, selectmode='browse', style='R.Treeview')
        self.rules_tree.heading('status', text='状态'); self.rules_tree.column('status', width=45, anchor='center')
        self.rules_tree.heading('chat', text='聊天对象'); self.rules_tree.column('chat', width=110)
        self.rules_tree.heading('keyword', text='关键词'); self.rules_tree.column('keyword', width=75)
        self.rules_tree.heading('reply', text='回复内容'); self.rules_tree.column('reply', width=130)
        self.rules_tree.tag_configure('on', background=self.C['soft_green'])
        self.rules_tree.tag_configure('off', background=self.C['row_even'])
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
                           ('last_message','最后检测消息'),('last_trigger','最后触发'),('last_send','最后发送'),
                           ('sent_ok','成功回复'),('sent_fail','失败回复'),('last_record_time','最后记录')]:
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

        # Log + records
        _, lgc = self._card(bottom, '运行日志 / 回复记录'); lgc.pack(fill=tk.BOTH, expand=True)
        self.history_tabs = self.ttk.Notebook(lgc)
        self.history_tabs.pack(fill=tk.BOTH, expand=True)
        log_tab = tk.Frame(self.history_tabs, bg=self.C['card'])
        records_tab = tk.Frame(self.history_tabs, bg=self.C['card'])
        self.history_tabs.add(log_tab, text='运行日志')
        self.history_tabs.add(records_tab, text='回复记录')

        lbf = tk.Frame(log_tab, bg=self.C['card']); lbf.pack(fill=tk.X, pady=(0, 4))
        self._bsm(lbf, '清空日志', lambda: self.log_text.delete('1.0', tk.END) if self.log_text else None, 'text2', 'text2').pack(side=tk.LEFT, padx=1)
        self._bsm(lbf, '复制日志', self._copy_log, 'blue', 'blue_hov').pack(side=tk.LEFT, padx=3)
        ltf = tk.Frame(log_tab, bg=self.C['border'], padx=1, pady=1); ltf.pack(fill=tk.BOTH, expand=True)
        self.log_text = self.scrolledtext.ScrolledText(
            ltf, font=('Cascadia Mono', 9), relief=tk.FLAT, bd=0,
            wrap=tk.WORD, bg='#1A1E2B', fg='#D4D4D8', insertbackground='white')
        self.log_text.pack(fill=tk.BOTH, expand=True)
        for t, c in [('trigger','#FACC15'),('reply','#4ADE80'),('error','#F87171'),
                     ('info','#60A5FA'),('skip','#9CA3AF'),('poll','#A78BFA')]:
            self.log_text.tag_configure(t, foreground=c)

        rbf = tk.Frame(records_tab, bg=self.C['card']); rbf.pack(fill=tk.X, pady=(0, 4))
        self._bsm(rbf, '刷新记录', self._refresh_records_tree, 'primary', 'primary_hov').pack(side=tk.LEFT, padx=1)
        self._bsm(rbf, '复制选中', self._copy_selected_record, 'blue', 'blue_hov').pack(side=tk.LEFT, padx=3)
        self._bsm(rbf, '清空记录', self._clear_records, 'danger', 'danger_hov').pack(side=tk.LEFT, padx=3)
        rtf = tk.Frame(records_tab, bg=self.C['border'], padx=1, pady=1); rtf.pack(fill=tk.BOTH, expand=True)
        record_cols = ('time', 'chat', 'rule', 'keyword', 'reply', 'status')
        self.records_tree = tk.ttk.Treeview(rtf, columns=record_cols, show='headings',
                                            height=7, selectmode='browse', style='R.Treeview')
        widths = {'time':125, 'chat':135, 'rule':60, 'keyword':110, 'reply':280, 'status':70}
        labels = {'time':'回复时间', 'chat':'聊天对象', 'rule':'规则', 'keyword':'关键词',
                  'reply':'回复内容', 'status':'状态'}
        for col in record_cols:
            self.records_tree.heading(col, text=labels[col])
            self.records_tree.column(col, width=widths[col], anchor='center' if col in ('rule','status') else 'w')
        self.records_tree.tag_configure('ok', background=self.C['soft_green'])
        self.records_tree.tag_configure('fail', background=self.C['soft_red'])
        self.records_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rsb = tk.Scrollbar(rtf, orient=tk.VERTICAL, command=self.records_tree.yview,
                           bg=self.C['card'])
        self.records_tree.configure(yscrollcommand=rsb.set)
        rsb.pack(side=tk.RIGHT, fill=tk.Y)

    # ===== UI 逻辑 =====

    def _copy_log(self):
        if not self.log_text:
            return
        text = self.log_text.get('1.0', self.tk.END).strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.engine.log('[UI] 已复制运行日志')

    def _refresh_records_tree(self):
        if not self.records_tree:
            return
        children = self.records_tree.get_children()
        if children:
            self.records_tree.delete(*children)
        records = self.engine.load_reply_records(limit=200)
        for i, rec in enumerate(records):
            ok = bool(rec.get('success'))
            tag = 'ok' if ok else 'fail'
            t = rec.get('time', '')
            if 'T' in t:
                t = t.replace('T', ' ')
            self.records_tree.insert('', self.tk.END, iid='rec{}'.format(i),
                                     values=(t, rec.get('chat', ''),
                                             '#{}'.format(rec.get('rule_index', '')),
                                             rec.get('keyword', ''),
                                             rec.get('reply', ''),
                                             '成功' if ok else '失败'),
                                     tags=(tag,))
        self._records_seen_version = self.engine.records_version

    def _copy_selected_record(self):
        if not self.records_tree:
            return
        sel = self.records_tree.selection()
        if not sel:
            self.messagebox.showinfo('提示', '请先选择一条回复记录')
            return
        vals = self.records_tree.item(sel[0], 'values')
        text = '时间: {}\n聊天对象: {}\n规则: {}\n关键词: {}\n回复内容: {}\n状态: {}'.format(*vals)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.engine.log('[UI] 已复制选中回复记录')

    def _clear_records(self):
        if self.messagebox.askyesno('确认', '清空所有回复记录?'):
            if self.engine.clear_reply_records():
                self._refresh_records_tree()

    def _append_log(self, msg):
        if not self.log_text: return
        for kw, tag in [('[KEYWORD]','trigger'),('[REPLY]','reply'),('[SEND]','reply'),
                        ('失败','error'),('异常','error'),
                        ('[START]','info'),('[STOP]','info'),('[LOOP]','info'),
                        ('[END]','info'),('[RUN]','info'),('[MODE]','info'),
                        ('[CONFIG]','info'),('[UI]','info'),('[RECORD]','info'),
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
        children = self.rules_tree.get_children()
        if children:
            self.rules_tree.delete(*children)
        for i, r in enumerate(self.engine.rules):
            status = '[ON]' if r.enabled else '[OFF]'
            self.rules_tree.insert('', self.tk.END, iid=str(i),
                                   values=(status, r.chat, r.keyword, r.reply),
                                   tags=('on' if r.enabled else 'off',))

    def _refresh_all(self):
        self._refresh_rules_tree()
        self._refresh_records_tree()
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

        # 只显示当前状态下需要的操作
        if self.engine.monitor_running:
            self.btn_stop.pack(side=self.tk.LEFT, padx=3)
        else:
            if self.engine.mode == 'loop':
                self.btn_start_loop.pack(side=self.tk.LEFT, padx=3)
            else:
                self.btn_start_once.pack(side=self.tk.LEFT, padx=3)

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
            return False
        try:
            p = int(self.entry_pause.get().strip())
            if p < 0: raise ValueError
            self.engine.pause_duration = p
        except:
            self.messagebox.showwarning('错误', '暂停时长必须是 >=0 的整数')
            return False
        try:
            v = float(self.entry_interval.get().strip())
            if v < 0: raise ValueError
            self.engine.poll_interval = v
        except:
            self.messagebox.showwarning('错误', '间隔必须是 >=0 的数字')
            return False
        self.engine.save_config()
        self.engine.log('[CONFIG] 激活{}秒/暂停{}秒 间隔{}秒'.format(a, p, v))
        return True

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
        if self.engine.update_rule(idx, c, k, r):
            self._refresh_rules_tree()
            self._clear_form()

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
        self.engine.set_all_rules(enabled)
        self._refresh_rules_tree()
        self._update_hint()

    def _start_once(self):
        if self.engine.start(mode='once'):
            self._set_ui_running()

    def _start_loop(self):
        if not self._apply_loop_params():  # 先应用参数
            return
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
        enabled = self.engine.enabled_rule_count()
        if self.header_rules:
            self.header_rules.config(text='{} 条启用规则'.format(enabled))
        labels['monitor'].config(text='运行中' if running else '已停止',
                                 fg=self.C['primary'] if running else self.C['danger'])
        self.header_badge.config(text='运行中' if running else '已停止',
                                 bg=self.C['primary'] if running else self.C['danger'])
        self._pulse_on = not self._pulse_on if running else False
        led_color = self.C['primary'] if self._pulse_on else self.C['cyan']
        self.led.config(text='● 运行中' if running else '● 已停止',
                        fg=led_color if running else self.C['danger'])
        labels['mode'].config(text=st.get('mode','-'))
        labels['current_chat'].config(text=(st.get('current_chat','-') or '-')[:25])
        labels['last_message'].config(text=(st.get('last_message','-') or '-')[:30])
        labels['last_trigger'].config(text=(st.get('last_trigger','-') or '-')[:30])
        labels['last_send'].config(text=(st.get('last_send','-') or '-')[:20])
        labels['sent_ok'].config(text=str(st.get('sent_ok', 0)), fg=self.C['primary'])
        labels['sent_fail'].config(text=str(st.get('sent_fail', 0)), fg=self.C['danger'])
        labels['last_record_time'].config(text=st.get('last_record_time', '') or '-')
        rn = st.get('round',0); total = st.get('total_rounds',0)
        if running and total == '-': self.round_label.config(text='第 {} 轮'.format(rn))
        elif running: self.round_label.config(text='第 {}/{} 轮'.format(rn, total))
        else: self.round_label.config(text='')
        self._update_start_button()
        self._update_hint()
        try: GlobalHotkey.pump_once()  # 主线程处理全局热键
        except: pass
        self.root.after(1000, self._update_status)

    def _update_hint(self):
        enabled = self.engine.enabled_rule_count()
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
        if self.records_tree and self._records_seen_version != self.engine.records_version:
            self._refresh_records_tree()
        try: GlobalHotkey.pump_once()  # 高频检查热键(300ms)
        except: pass
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
