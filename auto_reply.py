"""
wxauto 4.1 自动回复监控系统 v4
--------------------------------
模式: 检测一次 / 循环检测
多规则: 每规则独立 (聊天+关键词+回复)
预设: 保存/加载/删除 配置快照
"""
import sys, os, json, time, threading, queue, hashlib, copy
from datetime import datetime

import pyperclip
import win32gui, win32con, win32api
import uiautomation as uia


# ==================== COM / 底层工具 ====================

def init_com():
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except ImportError:
        pass

def focus_wechat():
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
    if hwnd:
        try: win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except: pass
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
    def _win():
        h = get_hwnd()
        if not h: return None
        init_com()
        try: return uia.ControlFromHandle(h)
        except:
            try: return uia.WindowControl(ClassName='mmui::MainWindow', searchDepth=1)
            except: return None

    @staticmethod
    def _nav_splitter():
        w = FreshNav._win()
        if not w: return None
        r = w.GroupControl(searchDepth=1)
        s = r.CustomControl(ClassName='QStackedWidget')
        m = s.GroupControl(ClassName='mmui::MainView')
        cw = [c for c in m.GetChildren() if c.ClassName=='QWidget' and c.ControlTypeName=='GroupControl']
        if not cw: return None
        im = cw[0].GroupControl(ClassName='mmui::MainView')
        sp = im.CustomControl(ClassName='mmui::XSplitterView')
        os = sp.CustomControl(ClassName='mmui::XStackedWidget')
        return os.CustomControl(ClassName='mmui::XSplitterView')

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
        """打开指定会话: 可见列表→慢速翻页→搜索框"""
        init_com()
        focus_wechat()
        time.sleep(0.05)
        uia.SetGlobalSearchTimeout(2)

        # 快速路径: 当前已是目标聊天
        try:
            cur_name, _ = FreshNav.get_message_items()
            if cur_name and cur_name.strip() == who.strip():
                self.status['current_chat'] = cur_name
                return True
        except: pass

        # ---- 方法1: 可见列表 + 慢速翻页扫描 ----
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
        init_com()
        rect = FreshNav.get_input_rect()
        if not rect:
            self.log('[SEND] 找不到输入框')
            return False
        mouse_click((rect[0]+rect[2])//2, (rect[1]+rect[3])//2)
        time.sleep(0.08)
        pyperclip.copy(text)
        time.sleep(0.05)
        uia.SendKeys('{Ctrl}v', waitTime=0.03)
        time.sleep(0.08)
        uia.SendKeys('{Enter}', waitTime=0.03)
        time.sleep(0.08)
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

                self.status['round'] += 1
                self.status['total_rounds'] = '-'
                self._poll_all_rules(rules, is_once=False)
                time.sleep(max(self.poll_interval, 0.1))  # 最少100ms防止空转

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

            # 2. 取最新一条 incoming
            cur_name, items = FreshNav.get_message_items()
            if not cur_name or cur_name.strip() != chat.strip():
                self.log('[SKIP] 聊天不匹配: 期望[{}] 实际[{}]'.format(chat, cur_name))
                continue

            latest = None
            for item in reversed(list(items)):
                msg = RawMsg(item)
                if msg.is_timestamp or not msg.name.strip():
                    continue
                if self._is_self(msg.name):
                    continue
                latest = msg
                break

            if latest is None:
                self.log('[POLL] 聊天: {} 无 incoming'.format(chat))
                continue

            # 3. 和上次相同→跳过
            prev = self.last_incoming.get(chat, '')
            if latest.name.strip() == prev:
                self.log('[DEDUP] 最后消息未变 {!r} -> SKIP'.format(prev[:30]))
                continue
            self.last_incoming[chat] = latest.name.strip()

            self.log('[POLL] 聊天: {} 最后消息: {!r}'.format(chat, latest.name[:60]))
            self.status['current_chat'] = chat
            self.status['last_message'] = latest.name[:60]

            # 4. 逐条规则匹配, 命中第一条即回复并停止
            for idx, rule in rule_list:
                if not self._match_kw(latest.name, rule.keyword):
                    continue
                if not rule.reply.strip():
                    continue
                if rule.reply.strip() in latest.name:
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

    def __init__(self, engine):
        self.engine = engine
        engine.load_config()
        self.root = None
        self.log_text = None

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
        self.root.title('wxauto 自动回复监控 v4')
        self.root.geometry('900x750')
        self.root.minsize(800, 600)
        self.root.configure(bg='#f0f0f0')

        self._build_ui()
        self._refresh_all()
        self._update_status()
        self._pull_logs()

        def on_close():
            self.engine.stop()
            try: self.root.destroy()
            except: pass
        self.root.protocol('WM_DELETE_WINDOW', on_close)

        # Ctrl+C 强制停止
        def force_stop(e=None):
            self.engine.log('[CTRL+C] 强制中断')
            self.engine.stop()
        self.root.bind_all('<Control-c>', force_stop)

        # 系统 signal (控制台 Ctrl+C, 即使窗口未聚焦也生效)
        try:
            import signal
            signal.signal(signal.SIGINT, lambda sig, frame: force_stop())
        except:
            pass

        self.root.mainloop()

    def _build_ui(self):
        tk = self.tk

        # ===== 标题 =====
        tf = tk.Frame(self.root, bg='#2c3e50', height=42)
        tf.pack(fill=tk.X); tf.pack_propagate(False)
        tk.Label(tf, text='wxauto 自动回复监控 v4', bg='#2c3e50', fg='white',
                 font=('Microsoft YaHei', 13, 'bold')).pack(pady=8)

        # ===== 模式选择行 =====
        mode_bar = tk.Frame(self.root, bg='#34495e')
        mode_bar.pack(fill=tk.X, padx=8, pady=(5,0))

        tk.Label(mode_bar, text='运行模式:', bg='#34495e', fg='white',
                 font=('Microsoft YaHei', 10, 'bold')).pack(side=tk.LEFT, padx=8, pady=6)

        self.mode_var = tk.StringVar(value=self.engine.mode)
        rb_once = tk.Radiobutton(mode_bar, text='检测一次', variable=self.mode_var,
                                 value='once', command=self._on_mode_change,
                                 bg='#34495e', fg='white', selectcolor='#34495e',
                                 activebackground='#34495e', activeforeground='white',
                                 font=('Microsoft YaHei', 10))
        rb_once.pack(side=tk.LEFT, padx=5)
        rb_loop = tk.Radiobutton(mode_bar, text='循环检测', variable=self.mode_var,
                                 value='loop', command=self._on_mode_change,
                                 bg='#34495e', fg='white', selectcolor='#34495e',
                                 activebackground='#34495e', activeforeground='white',
                                 font=('Microsoft YaHei', 10))
        rb_loop.pack(side=tk.LEFT, padx=5)

        # 循环参数 (仅在loop模式显示)
        self.loop_params_frame = tk.Frame(mode_bar, bg='#34495e')
        self.loop_params_frame.pack(side=tk.LEFT, padx=20)
        tk.Label(self.loop_params_frame, text='激活(秒):', bg='#34495e', fg='white',
                 font=('Microsoft YaHei', 9)).pack(side=tk.LEFT)
        self.entry_active = tk.Entry(self.loop_params_frame, font=('Microsoft YaHei', 9),
                                      width=4, relief=tk.SOLID, bd=1)
        self.entry_active.pack(side=tk.LEFT, padx=2)
        self.entry_active.insert(0, str(self.engine.active_duration))
        tk.Label(self.loop_params_frame, text='暂停(秒):', bg='#34495e', fg='white',
                 font=('Microsoft YaHei', 9)).pack(side=tk.LEFT, padx=(8,0))
        self.entry_pause = tk.Entry(self.loop_params_frame, font=('Microsoft YaHei', 9),
                                     width=4, relief=tk.SOLID, bd=1)
        self.entry_pause.pack(side=tk.LEFT, padx=2)
        self.entry_pause.insert(0, str(self.engine.pause_duration))
        tk.Label(self.loop_params_frame, text='间隔(秒):', bg='#34495e', fg='white',
                 font=('Microsoft YaHei', 9)).pack(side=tk.LEFT, padx=(8,0))
        self.entry_interval = tk.Entry(self.loop_params_frame, font=('Microsoft YaHei', 9),
                                       width=4, relief=tk.SOLID, bd=1)
        self.entry_interval.pack(side=tk.LEFT, padx=2)
        self.entry_interval.insert(0, str(self.engine.poll_interval))
        tk.Button(self.loop_params_frame, text='应用', command=self._apply_loop_params,
                  bg='#3498db', fg='white', font=('Microsoft YaHei', 8),
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=5)

        # 预设按钮
        preset_frame = tk.Frame(mode_bar, bg='#34495e')
        preset_frame.pack(side=tk.RIGHT, padx=8)
        tk.Button(preset_frame, text='保存预设', command=self._save_preset,
                  bg='#8e44ad', fg='white', font=('Microsoft YaHei', 8),
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)
        tk.Button(preset_frame, text='加载预设', command=self._load_preset,
                  bg='#8e44ad', fg='white', font=('Microsoft YaHei', 8),
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)
        tk.Button(preset_frame, text='删除预设', command=self._delete_preset,
                  bg='#c0392b', fg='white', font=('Microsoft YaHei', 8),
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)

        self._update_loop_params_visibility()

        # ===== 主区域 =====
        main = tk.Frame(self.root, bg='#f0f0f0')
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=5)

        # ---- 左侧: 规则列表 ----
        left = tk.LabelFrame(main, text=' 回复规则列表 ', font=('Microsoft YaHei', 10),
                            bg='#f0f0f0', padx=5, pady=5)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0,5))

        # treeview 表格式
        columns = ('status','chat','keyword','reply')
        self.rules_tree = tk.ttk.Treeview(left, columns=columns, show='headings',
                                           height=12, selectmode='browse')
        self.rules_tree.heading('status', text='状态', anchor='center')
        self.rules_tree.heading('chat', text='聊天对象')
        self.rules_tree.heading('keyword', text='关键词')
        self.rules_tree.heading('reply', text='回复内容')
        self.rules_tree.column('status', width=40, anchor='center')
        self.rules_tree.column('chat', width=100)
        self.rules_tree.column('keyword', width=70)
        self.rules_tree.column('reply', width=120)
        self.rules_tree.pack(fill=tk.BOTH, expand=True)
        self.rules_tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        # 滚动条
        tsb = tk.Scrollbar(left, orient=tk.VERTICAL, command=self.rules_tree.yview)
        self.rules_tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side=tk.RIGHT, fill=tk.Y)

        # 操作按钮
        btnf = tk.Frame(left, bg='#f0f0f0')
        btnf.pack(fill=tk.X, pady=(5,0))
        tk.Button(btnf, text='删除选中', command=self._del_rule,
                  bg='#e74c3c', fg='white', font=('Microsoft YaHei', 9),
                  relief=tk.FLAT, padx=8).pack(side=tk.LEFT, padx=2)
        tk.Button(btnf, text='启用/禁用', command=self._toggle_rule,
                  bg='#f39c12', fg='white', font=('Microsoft YaHei', 9),
                  relief=tk.FLAT, padx=8).pack(side=tk.LEFT, padx=2)
        tk.Button(btnf, text='全部启用', command=lambda: self._all_toggle(True),
                  bg='#27ae60', fg='white', font=('Microsoft YaHei', 8),
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)
        tk.Button(btnf, text='全部禁用', command=lambda: self._all_toggle(False),
                  bg='#95a5a6', fg='white', font=('Microsoft YaHei', 8),
                  relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=2)

        # 状态面板
        sf = tk.LabelFrame(left, text=' 运行状态 ', font=('Microsoft YaHei', 10),
                           bg='#f0f0f0', padx=5, pady=5)
        sf.pack(fill=tk.X, pady=(8,0))
        self.status_labels = {}
        for key, label in [
            ('monitor','监控状态'), ('mode','运行模式'),
            ('current_chat','当前聊天'), ('last_message','最后检测消息'),
            ('last_trigger','最后触发'), ('last_send','最后发送'),
        ]:
            row = tk.Frame(sf, bg='#f0f0f0')
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=label+':', bg='#f0f0f0', font=('Microsoft YaHei', 8),
                     width=11, anchor='e').pack(side=tk.LEFT)
            val = tk.Label(row, text='-', bg='#f0f0f0',
                           font=('Microsoft YaHei', 8, 'bold'), fg='#555', anchor='w')
            val.pack(side=tk.LEFT, padx=(3,0))
            self.status_labels[key] = val
        self.hint_label = tk.Label(sf, text='', bg='#f0f0f0',
                                   font=('Microsoft YaHei', 8), fg='#e67e22', wraplength=280)
        self.hint_label.pack(fill=tk.X, pady=(5,0))

        # ---- 右侧: 编辑区 ----
        right = tk.LabelFrame(main, text=' 添加/编辑规则 ', font=('Microsoft YaHei', 10),
                             bg='#f0f0f0', padx=5, pady=5)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(5,0))

        tk.Label(right, text='聊天对象名:', bg='#f0f0f0',
                 font=('Microsoft YaHei', 9), anchor='w').pack(fill=tk.X, pady=(5,2))
        self.entry_chat = tk.Entry(right, font=('Microsoft YaHei', 10), relief=tk.SOLID, bd=1)
        self.entry_chat.pack(fill=tk.X, ipady=3)

        tk.Label(right, text='触发关键词:', bg='#f0f0f0',
                 font=('Microsoft YaHei', 9), anchor='w').pack(fill=tk.X, pady=(8,2))
        self.entry_keyword = tk.Entry(right, font=('Microsoft YaHei', 10), relief=tk.SOLID, bd=1)
        self.entry_keyword.pack(fill=tk.X, ipady=3)

        tk.Label(right, text='自动回复内容:', bg='#f0f0f0',
                 font=('Microsoft YaHei', 9), anchor='w').pack(fill=tk.X, pady=(8,2))
        self.text_reply = tk.Text(right, font=('Microsoft YaHei', 10), height=4,
                                  relief=tk.SOLID, bd=1, wrap=tk.WORD)
        self.text_reply.pack(fill=tk.X, pady=(0,5))

        brf = tk.Frame(right, bg='#f0f0f0')
        brf.pack(fill=tk.X, pady=(5,0))
        tk.Button(brf, text=' 添加规则 ', command=self._add_rule, bg='#27ae60', fg='white',
                  font=('Microsoft YaHei', 10, 'bold'), relief=tk.FLAT, padx=12, pady=4
                  ).pack(side=tk.LEFT, padx=2)
        tk.Button(brf, text=' 更新选中 ', command=self._update_rule, bg='#2980b9', fg='white',
                  font=('Microsoft YaHei', 10, 'bold'), relief=tk.FLAT, padx=12, pady=4
                  ).pack(side=tk.LEFT, padx=2)
        tk.Button(brf, text=' 清空 ', command=self._clear_form, bg='#95a5a6', fg='white',
                  font=('Microsoft YaHei', 9), relief=tk.FLAT, padx=8, pady=4
                  ).pack(side=tk.LEFT, padx=2)

        # ===== 底部: 控制 + 日志 =====
        bottom = tk.Frame(self.root, bg='#f0f0f0')
        bottom.pack(fill=tk.X, padx=8, pady=(0,5))

        ctrl = tk.Frame(bottom, bg='#f0f0f0')
        ctrl.pack(fill=tk.X, pady=(0,5))

        self.btn_start_once = tk.Button(ctrl, text='  检测一次  ', command=self._start_once,
                                        bg='#2980b9', fg='white',
                                        font=('Microsoft YaHei', 10, 'bold'),
                                        relief=tk.FLAT, padx=12, pady=5)
        self.btn_start_loop = tk.Button(ctrl, text='  循环检测  ', command=self._start_loop,
                                        bg='#27ae60', fg='white',
                                        font=('Microsoft YaHei', 10, 'bold'),
                                        relief=tk.FLAT, padx=12, pady=5)

        self.btn_stop = tk.Button(ctrl, text='  停止  ', command=self._stop,
                                  bg='#e74c3c', fg='white',
                                  font=('Microsoft YaHei', 10, 'bold'),
                                  relief=tk.FLAT, padx=12, pady=5, state=tk.DISABLED)

        self.led = tk.Label(ctrl, text='O 已停止', font=('Microsoft YaHei', 10, 'bold'),
                            bg='#f0f0f0', fg='#e74c3c')
        self.led.pack(side=tk.LEFT, padx=15)

        self.round_label = tk.Label(ctrl, text='',
                                    font=('Microsoft YaHei', 9), bg='#f0f0f0', fg='#555')
        self.round_label.pack(side=tk.LEFT, padx=10)

        # 根据当前模式只显示对应按钮
        self._update_start_button()

        # 日志
        lgf = tk.LabelFrame(bottom, text=' 运行日志 ', font=('Microsoft YaHei', 9),
                           bg='#f0f0f0', padx=3, pady=3)
        lgf.pack(fill=tk.BOTH, expand=True)
        self.log_text = self.scrolledtext.ScrolledText(
            lgf, font=('Consolas', 9), height=10, relief=tk.SOLID, bd=1,
            wrap=tk.WORD, bg='#1e1e1e', fg='#d4d4d4')
        self.log_text.pack(fill=tk.BOTH, expand=True)
        for tag, color in [('trigger','#f1c40f'),('reply','#2ecc71'),('error','#e74c3c'),
                           ('info','#3498db'),('skip','#95a5a6'),('poll','#8e44ad')]:
            self.log_text.tag_configure(tag, foreground=color)

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
        c = self.entry_chat.get().strip()
        k = self.entry_keyword.get().strip()
        r = self.text_reply.get('1.0', self.tk.END).strip()
        if not c or not k or not r:
            self.messagebox.showwarning('提示', '请填写完整')
            return
        self.engine.add_rule(c, k, r)
        self._refresh_rules_tree()
        self._clear_form()
        self._update_hint()

    def _update_rule(self):
        sel = self.rules_tree.selection()
        if not sel: return
        idx = int(sel[0])
        if idx >= len(self.engine.rules): return
        c = self.entry_chat.get().strip()
        k = self.entry_keyword.get().strip()
        r = self.text_reply.get('1.0', self.tk.END).strip()
        if not c or not k or not r:
            self.messagebox.showwarning('提示', '请填写完整')
            return
        with self.engine._lock:
            self.engine.rules[idx].chat = c
            self.engine.rules[idx].keyword = k
            self.engine.rules[idx].reply = r
        self.engine.save_config()
        self._refresh_rules_tree()
        self._clear_form()
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
        if self.engine.mode == 'loop':
            self.btn_start_loop.config(state=self.tk.DISABLED)
        else:
            self.btn_start_once.config(state=self.tk.DISABLED)
        self.btn_stop.config(state=self.tk.NORMAL)

    def _set_ui_stopped(self):
        if self.engine.mode == 'loop':
            self.btn_start_loop.config(state=self.tk.NORMAL)
        else:
            self.btn_start_once.config(state=self.tk.NORMAL)
        self.btn_stop.config(state=self.tk.DISABLED)

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
        st = self.engine.status
        labels = self.status_labels
        running = self.engine.monitor_running
        labels['monitor'].config(text='运行中' if running else '已停止',
                                 fg='#27ae60' if running else '#e74c3c')
        self.led.config(text='O 运行中' if running else 'O 已停止',
                        fg='#27ae60' if running else '#e74c3c')
        labels['mode'].config(text=st.get('mode','-'))
        labels['current_chat'].config(text=(st.get('current_chat','-') or '-')[:25])
        labels['last_message'].config(text=(st.get('last_message','-') or '-')[:30])
        labels['last_trigger'].config(text=(st.get('last_trigger','-') or '-')[:30])
        labels['last_send'].config(text=(st.get('last_send','-') or '-')[:20])

        round_num = st.get('round',0)
        total = st.get('total_rounds',0)
        if running and total == '-':
            self.round_label.config(text='第{}轮'.format(round_num))
        elif running:
            self.round_label.config(text='第{}/{}轮'.format(round_num, total))
        else:
            self.round_label.config(text='')

        if not running:
            self._set_ui_stopped()
        self._update_hint()
        self.root.after(1000, self._update_status)

    def _update_hint(self):
        enabled = sum(1 for r in self.engine.rules if r.enabled)
        running = self.engine.monitor_running
        if enabled and not running:
            self.hint_label.config(
                text='已配置{}条规则，点击[检测一次]或[循环检测]开始'.format(enabled))
        elif not enabled:
            self.hint_label.config(text='还没有规则，请先在右侧添加并启用。')
        elif running:
            mode = '循环' if self.engine.mode=='loop' else '单次'
            self.hint_label.config(text='{}检测中，{}条规则启用。'.format(mode, enabled))
        else:
            self.hint_label.config(text='')

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
            import signal
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
