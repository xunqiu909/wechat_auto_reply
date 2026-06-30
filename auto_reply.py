"""
wxauto 4.1 自动回复监控系统 v3
--------------------------------
键盘+UIA混合方案，完整日志链路，方向检测，去重，防循环
"""
import sys, os, json, re, time, threading, queue, hashlib
from datetime import datetime

import pyperclip
import win32gui, win32con, win32api
import uiautomation as uia


# ==================== COM / 工具 ====================

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


# ==================== UIA 导航(每次从HWND重建) ====================

class FreshNav:
    """每次调用都从 HWND 全新建立 UIA 树, 杜绝stale引用"""

    @staticmethod
    def _win():
        h = get_hwnd()
        if not h: return None
        init_com()
        try:
            return uia.ControlFromHandle(h)
        except:
            try:
                return uia.WindowControl(ClassName='mmui::MainWindow', searchDepth=1)
            except:
                return None

    @staticmethod
    def _nav_to_splitter():
        """导航到右侧 XStackedWidget 下的 XSplitterView"""
        win = FreshNav._win()
        if not win: return None
        root = win.GroupControl(searchDepth=1)
        stacked = root.CustomControl(ClassName='QStackedWidget')
        mv = stacked.GroupControl(ClassName='mmui::MainView')
        cws = [c for c in mv.GetChildren() if c.ClassName == 'QWidget' and c.ControlTypeName == 'GroupControl']
        if not cws: return None
        imv = cws[0].GroupControl(ClassName='mmui::MainView')
        s = imv.CustomControl(ClassName='mmui::XSplitterView')
        outer_stack = s.CustomControl(ClassName='mmui::XStackedWidget')
        outer_splitter = outer_stack.CustomControl(ClassName='mmui::XSplitterView')
        return outer_splitter

    @staticmethod
    def get_right_splitter():
        """返回右侧 ChatDetailView -> 聊天页 -> XSplitterView(消息+输入)"""
        outer = FreshNav._nav_to_splitter()
        if not outer: return None
        rights = [c for c in outer.GetChildren() if c.ClassName == 'mmui::XStackedWidget']
        if not rights: return None
        cdd = rights[0].GroupControl(ClassName='mmui::ChatDetailView')
        chat_page = cdd.GetFirstChildControl()
        if chat_page.ClassName != 'mmui::ChatMessagePage':
            return None  # 没有聊天打开
        return chat_page.CustomControl(ClassName='mmui::XSplitterView')  # 消息+输入

    @staticmethod
    def get_message_items():
        """返回 (chat_name, [msg_items]) 或 (None, [])"""
        outer = FreshNav._nav_to_splitter()
        if not outer: return None, []
        rights = [c for c in outer.GetChildren() if c.ClassName == 'mmui::XStackedWidget']
        if not rights: return None, []
        cdd = rights[0].GroupControl(ClassName='mmui::ChatDetailView')
        chat_page = cdd.GetFirstChildControl()
        if chat_page.ClassName != 'mmui::ChatMessagePage':
            return None, []

        # 标题栏 -> 聊天对象名
        tb = chat_page.GetChildren()[0]
        def find_name(ele):
            try:
                if (ele.ControlTypeName == 'TextControl' and
                    ele.ClassName == 'mmui::XTextView'):
                    n = ele.Name or ''
                    if n and not (n.startswith('(') and n.endswith(')')):
                        return n
                for c in ele.GetChildren():
                    r = find_name(c)
                    if r: return r
            except: pass
            return None
        chat_name = find_name(tb)

        # 消息区
        splitter = chat_page.CustomControl(ClassName='mmui::XSplitterView')
        mvs = [c for c in splitter.GetChildren() if c.ClassName == 'mmui::MessageView']
        if not mvs: return chat_name, []
        msg_list = mvs[0].ListControl(ClassName='mmui::RecyclerListView')
        return chat_name, msg_list.GetChildren()

    @staticmethod
    def get_session_table():
        """返回会话列表 XTableView"""
        outer = FreshNav._nav_to_splitter()
        if not outer: return None
        lefts = [c for c in outer.GetChildren() if c.ClassName == 'mmui::XView']
        if not lefts: return None
        cmv = lefts[0].GroupControl(ClassName='mmui::ChatMasterView')
        cli = cmv.GroupControl(ClassName='mmui::XView')
        areas = [c for c in cli.GetChildren() if c.ClassName == 'mmui::XView']
        if len(areas) < 2: return None
        li = areas[1].GroupControl(ClassName='mmui::XView')
        sl = li.GroupControl(ClassName='mmui::ChatSessionList')
        return sl.ListControl(ClassName='mmui::XTableView')

    @staticmethod
    def get_input_rect():
        """返回输入框区域坐标"""
        splitter = FreshNav.get_right_splitter()
        if not splitter: return None
        xvs = [c for c in splitter.GetChildren() if c.ClassName == 'mmui::XView']
        if not xvs: return None
        r = xvs[0].BoundingRectangle
        return (r.left, r.top, r.right, r.bottom)

    @staticmethod
    def switch_to_chat_tab():
        win = FreshNav._win()
        if not win: return
        root = win.GroupControl(searchDepth=1)
        stacked = root.CustomControl(ClassName='QStackedWidget')
        mv = stacked.GroupControl(ClassName='mmui::MainView')
        nav = mv.ToolBarControl(ClassName='mmui::MainTabBar')
        nav.ButtonControl(Name='微信').Click(simulateMove=False)


# ==================== 消息模型 ====================

class RawMsg:
    """原始消息"""
    __slots__ = ('cls', 'name', 'runtime_id', 'rect_left', 'rect_right')
    def __init__(self, item):
        self.cls = item.ClassName
        self.name = item.Name or ''
        r = item.BoundingRectangle
        self.rect_left = r.left
        self.rect_right = r.right
        try:
            self.runtime_id = ''.join(str(i) for i in item.GetRuntimeId())
        except:
            self.runtime_id = ''

    @property
    def is_timestamp(self):
        return self.cls == 'mmui::ChatItemView'

    @property
    def is_text(self):
        return self.cls == 'mmui::ChatTextItemView'

    @property
    def content_hash(self):
        return hashlib.md5(self.name.encode('utf-8', errors='replace')).hexdigest()[:16]


# ==================== 监控引擎 ====================

class MonitorEngine:

    def __init__(self, poll_interval=3):
        self.tasks = {}               # {chat_name: {keywords, reply, enabled}}
        self.monitor_running = False  # 监控是否在运行(区别于任务enabled)
        self._thread = None
        self._lock = threading.Lock()
        self.poll_interval = poll_interval
        self.log_queue = queue.Queue()

        # 去重 & 防循环
        self.baseline_ids = {}        # {chat: set(runtime_id)}  启动时的消息基线
        self.replied_hashes = {}      # {chat: set(content_hash)}  已回复的消息
        self.sent_texts = set()       # 自己发过的所有文本(防循环)

        # UI 状态
        self.status = {
            'current_chat': '',        # 最后识别的聊天
            'last_message': '',        # 最后检测的消息
            'last_match_task': '',     # 最后匹配的任务
            'last_trigger_result': '', # 触发结果
            'last_send_result': '',    # 发送结果
            'last_skip_reason': '',    # 跳过原因
        }

        self.config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'auto_reply_config.json')

    def log(self, msg):
        t = datetime.now().strftime('%H:%M:%S')
        text = '[{}] {}'.format(t, msg)
        self.log_queue.put(text)
        try: print(text)
        except: print(text.encode('ascii', errors='replace').decode('ascii', errors='replace'))

    # ---- 配置 ----
    def load_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.tasks = data.get('tasks', {})
                self.poll_interval = data.get('poll_interval', 3)
                self.log('已加载 {} 个监控任务'.format(len(self.tasks)))
                return True
            except Exception as e:
                self.log('加载配置失败: {}'.format(e))
        return False

    def save_config(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump({'tasks': self.tasks, 'poll_interval': self.poll_interval},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log('保存配置失败: {}'.format(e))

    def add_task(self, chat_name, keywords, reply_text):
        with self._lock:
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(',') if k.strip()]
            self.tasks[chat_name] = {
                'keywords': keywords,
                'reply': reply_text,
                'enabled': True
            }
            self.save_config()
            self.log('添加监控: {} | {} | {}...'.format(chat_name, ','.join(keywords), reply_text[:25]))

    def remove_task(self, name):
        with self._lock:
            self.tasks.pop(name, None)
            self.baseline_ids.pop(name, None)
            self.replied_hashes.pop(name, None)
            self.save_config()
            self.log('移除监控: {}'.format(name))

    def toggle_task(self, name, enabled):
        with self._lock:
            if name in self.tasks:
                self.tasks[name]['enabled'] = enabled
                self.save_config()
                self.log('{}: {}'.format('启用' if enabled else '禁用', name))

    # ---- 核心操作 ----

    def _open_chat(self, who):
        """打开指定会话(滚动+点击)"""
        init_com()
        uia.SetGlobalSearchTimeout(3)

        # 先滚到顶部
        table = FreshNav.get_session_table()
        if table:
            for _ in range(20):
                table.WheelUp(wheelTimes=15, waitTime=0.01, interval=0.01)
                time.sleep(0.02)
            time.sleep(0.25)

        # 扫描
        seen = set()
        for direction in ['down', 'up']:
            for _ in range(12):
                table = FreshNav.get_session_table()
                if not table: return False
                items = table.GetChildren()
                cur = set()
                for item in items:
                    n = item.Name.split('\n')[0]
                    cur.add(n)
                    if who == n:
                        r = item.BoundingRectangle
                        mouse_click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
                        time.sleep(0.6)
                        if FreshNav.get_right_splitter():
                            return True
                        # 重试
                        mouse_click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
                        time.sleep(0.5)
                        return FreshNav.get_right_splitter() is not None
                if cur.issubset(seen): break
                seen.update(cur)
                if direction == 'down':
                    table.WheelDown(wheelTimes=15, waitTime=0.01, interval=0.01)
                else:
                    table.WheelUp(wheelTimes=15, waitTime=0.01, interval=0.01)
                time.sleep(0.25)
        return False

    def _send_reply(self, text):
        """发送回复(点击输入框 → 粘贴 → Enter)"""
        init_com()
        rect = FreshNav.get_input_rect()
        if not rect:
            self.log('[SEND] 找不到输入框区域')
            return False

        self.log('[SEND] 点击输入框')
        mouse_click((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        time.sleep(0.15)

        self.log('[SEND] 粘贴文本: {}'.format(text[:40]))
        pyperclip.copy(text)
        time.sleep(0.08)
        uia.SendKeys('{Ctrl}v', waitTime=0.05)
        time.sleep(0.15)

        self.log('[SEND] 按 Enter')
        uia.SendKeys('{Enter}', waitTime=0.05)
        time.sleep(0.15)

        # 记录自己发送的内容, 防止循环触发
        self.sent_texts.add(text.strip())
        self.log('[SEND] 发送成功')
        return True

    # ---- 消息方向判断 ----
    # 一对一聊天中, UIA 无法区分左右气泡
    # 策略: 任何在 sent_texts 中的内容 = outgoing(自己), 其余 = incoming(对方)

    def _is_self_message(self, text):
        """判断是否是自己发的消息"""
        t = text.strip()
        # 精确匹配
        if t in self.sent_texts:
            return True
        # 模糊匹配(剪贴板可能引入空白差异)
        for sent in self.sent_texts:
            if sent.strip() == t:
                return True
        return False

    def _normalize(self, text):
        """标准化文本"""
        if not text: return ''
        return text.strip().replace('\n', '').replace(' ', '')

    def _match_keyword(self, message_text, keyword):
        """关键词匹配(contains_match)"""
        if not message_text or not keyword:
            return False
        msg = self._normalize(message_text)
        kw = self._normalize(keyword)
        # contains match
        if kw and kw in msg:
            return True
        # exact match
        if msg == kw:
            return True
        return False

    # ---- 监控循环 ----

    def start(self):
        if self.monitor_running:
            self.log('[START] 监控已在运行中')
            return False

        # 确保微信在前台
        focus_wechat()
        time.sleep(0.3)

        # 清空历史状态
        self.baseline_ids.clear()
        self.replied_hashes.clear()
        self.sent_texts.clear()
        self.status = {
            'current_chat': '', 'last_message': '', 'last_match_task': '',
            'last_trigger_result': '', 'last_send_result': '', 'last_skip_reason': '',
        }

        self.monitor_running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

        with self._lock:
            enabled = [n for n, c in self.tasks.items() if c.get('enabled', True)]
        self.log('[START] 监控已启动，轮询间隔: {}秒'.format(self.poll_interval))
        self.log('[START] 当前启用任务数: {}'.format(len(enabled)))
        for n in enabled:
            cfg = self.tasks[n]
            self.log('[START] 监听聊天: {} | 触发关键词: {}'.format(n, ','.join(cfg['keywords'])))
        return True

    def stop(self):
        if not self.monitor_running:
            return
        self.monitor_running = False
        if self._thread:
            self._thread.join(timeout=5)
        self.log('[STOP] 监控已停止')

    def _build_baseline(self):
        """把当前所有可见消息加入基线, 避免启动时误触发"""
        with self._lock:
            active = {n: c for n, c in self.tasks.items() if c.get('enabled', True)}
        for chat_name in active:
            if not self._open_chat(chat_name):
                continue
            time.sleep(0.4)
            _, items = FreshNav.get_message_items()
            if items:
                ids = set()
                for item in items:
                    try:
                        ids.add(''.join(str(i) for i in item.GetRuntimeId()))
                    except:
                        pass
                self.baseline_ids[chat_name] = ids
                self.log('[BASELINE] {} : {} 条消息加入基线'.format(chat_name, len(ids)))

    def _loop(self):
        init_com()
        focus_wechat()
        uia.SetGlobalSearchTimeout(3)

        self.log('[LOOP] 监控循环启动')

        while self.monitor_running:
            try:
                with self._lock:
                    active = {n: c for n, c in self.tasks.items() if c.get('enabled', True)}

                if not active:
                    self.status['last_skip_reason'] = '没有启用的任务'
                    time.sleep(self.poll_interval)
                    continue

                for chat_name, cfg in active.items():
                    if not self.monitor_running:
                        break

                    # ====== Step 1: 打开聊天 ======
                    if not self._open_chat(chat_name):
                        self.log('[SKIP] 无法打开聊天: {}'.format(chat_name))
                        self.status['last_skip_reason'] = '无法打开聊天: {}'.format(chat_name)
                        time.sleep(0.5)
                        continue
                    time.sleep(0.4)

                    # ====== Step 2: 获取当前聊天名 ======
                    cur_name, items = FreshNav.get_message_items()
                    self.status['current_chat'] = cur_name or '(无法识别)'

                    if not cur_name:
                        self.log('[CHAT] 当前聊天识别失败, 无法匹配任务')
                        self.status['last_skip_reason'] = '聊天识别失败'
                        continue

                    self.log('[POLL] 当前聊天: {}'.format(cur_name))

                    # ====== Step 3: 聊天匹配 ======
                    chat_match = cur_name.strip() == chat_name.strip()
                    if not chat_match:
                        self.log('[SKIP] 当前聊天不匹配: 期望[{}] 实际[{}]'.format(chat_name, cur_name))
                        self.status['last_skip_reason'] = '聊天不匹配: {} != {}'.format(chat_name, cur_name)
                        continue

                    self.log('[MATCH] 聊天匹配: True')

                    # ====== Step 4: 初始化去重 ======
                    if chat_name not in self.baseline_ids:
                        self.baseline_ids[chat_name] = set()
                    if chat_name not in self.replied_hashes:
                        self.replied_hashes[chat_name] = set()

                    baseline = self.baseline_ids[chat_name]
                    replied = self.replied_hashes[chat_name]

                    # 首次建立基线
                    is_first_poll = (len(baseline) == 0)
                    if is_first_poll:
                        for item in items:
                            try:
                                baseline.add(''.join(str(i) for i in item.GetRuntimeId()))
                            except:
                                pass
                        self.log('[BASELINE] {} : {} 条消息加入基线'.format(chat_name, len(baseline)))
                        continue  # 第一轮不检查，等下一轮

                    # ====== Step 5: 遍历消息 ======
                    found_incoming = False
                    for item in items:
                        if not self.monitor_running: break
                        msg = RawMsg(item)

                        # 跳过时间标记
                        if msg.is_timestamp:
                            continue

                        # 跳过空消息
                        if not msg.name.strip():
                            continue

                        self.status['last_message'] = msg.name[:60]

                        # ====== Step 5a: 方向判断 ======
                        is_self = self._is_self_message(msg.name)
                        direction = 'outgoing' if is_self else 'incoming'
                        self.log('[POLL] 消息方向: {} 内容: {!r}'.format(direction, msg.name[:60]))

                        if is_self:
                            self.log('[SKIP] 消息来自自己, 不触发规则')
                            continue

                        # ====== Step 5b: 去重 (baseline + replied) ======
                        if msg.runtime_id in baseline:
                            self.log('[DEDUP] old=True reason=in_baseline id={}'.format(msg.runtime_id[:20]))
                            continue

                        ch = msg.content_hash
                        if ch in replied:
                            self.log('[DEDUP] old=True reason=already_replied hash={}'.format(ch))
                            continue

                        self.log('[DEDUP] new=True reason=not_in_seen')

                        # ====== Step 5c: 关键词匹配 ======
                        keywords = cfg['keywords']
                        reply_text = cfg['reply']
                        matched_kw = None
                        for kw in keywords:
                            if self._match_keyword(msg.name, kw):
                                matched_kw = kw
                                break

                        self.log('[MATCH] keyword=\'{}\' message={!r} result={}'.format(
                            ','.join(keywords), self._normalize(msg.name)[:40], matched_kw is not None))

                        if not matched_kw:
                            self.status['last_skip_reason'] = '关键词不匹配'
                            continue

                        # ====== Step 5d: 检查回复内容 ======
                        if not reply_text or not reply_text.strip():
                            self.log('[SKIP] 自动回复内容为空')
                            self.status['last_skip_reason'] = '回复内容为空'
                            continue

                        # ====== Step 5e: 防循环(回复内容已在消息中) ======
                        if reply_text.strip() in msg.name:
                            self.log('[SKIP] 自己发送的自动回复, 不触发规则')
                            self.status['last_skip_reason'] = '自动回复内容已在消息中(防循环)'
                            continue

                        # ====== Step 5f: 触发! ======
                        self.log('[KEYWORD] 触发! [{}] keyword={!r} message={!r}'.format(
                            chat_name, matched_kw, msg.name[:40]))
                        self.status['last_match_task'] = '{} | {}'.format(chat_name, matched_kw)
                        self.status['last_trigger_result'] = '成功'

                        # ====== Step 5g: 发送回复 ======
                        self.log('[SEND] 准备发送到聊天: {}'.format(chat_name))
                        self.log('[SEND] 回复内容: {}'.format(reply_text[:40]))
                        ok = self._send_reply(reply_text)
                        if ok:
                            self.status['last_send_result'] = '成功'
                            self.log('[REPLY] -> [{}]: {}'.format(chat_name, reply_text[:40]))
                        else:
                            self.status['last_send_result'] = '失败'
                            self.log('[SKIP] 发送失败')

                        # 标记已回复
                        replied.add(ch)
                        if len(replied) > 200:
                            self.replied_hashes[chat_name] = set(list(replied)[-100:])

                        found_incoming = True
                        break  # 每个聊天每次只回复一条

                    # 更新基线(把本次看到的所有消息ID加入)
                    new_ids = set()
                    for item in items:
                        try:
                            new_ids.add(''.join(str(i) for i in item.GetRuntimeId()))
                        except:
                            pass
                    self.baseline_ids[chat_name] = baseline | new_ids

                # 等待下次轮询
                time.sleep(self.poll_interval)

            except Exception as e:
                self.log('[LOOP] 循环异常: {}'.format(e))
                import traceback
                self.log(traceback.format_exc())
                time.sleep(self.poll_interval)

        self.log('[LOOP] 监控循环已退出')


# ==================== Tkinter UI ====================

class AutoReplyUI:

    def __init__(self, engine):
        self.engine = engine
        engine.load_config()
        self.root = None
        self.log_text = None
        self.status_vars = {}   # 动态状态变量

    def build_and_run(self):
        try:
            import tkinter as tk
            from tkinter import ttk, messagebox
            import tkinter.scrolledtext as st
        except Exception as e:
            print('Tkinter not available: {}'.format(e))
            self._console_mode()
            return

        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.scrolledtext = st

        self.root = tk.Tk()
        self.root.title('wxauto 自动回复监控')
        self.root.geometry('850x720')
        self.root.minsize(750, 550)
        self.root.configure(bg='#f0f0f0')

        self._build_ui()
        self._refresh_list()
        self._update_status()
        self._pull_logs()

        def on_close():
            self.engine.stop()
            try: self.root.destroy()
            except: pass
        self.root.protocol('WM_DELETE_WINDOW', on_close)
        self.root.mainloop()

    def _build_ui(self):
        tk = self.tk

        # === 标题栏 ===
        tf = tk.Frame(self.root, bg='#2c3e50', height=42)
        tf.pack(fill=tk.X)
        tf.pack_propagate(False)
        tk.Label(tf, text='wxauto 自动回复监控系统', bg='#2c3e50', fg='white',
                 font=('Microsoft YaHei', 13, 'bold')).pack(pady=8)

        # === 主区域 ===
        main = tk.Frame(self.root, bg='#f0f0f0')
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=5)

        # ---- 左侧: 任务列表 ----
        left = tk.LabelFrame(main, text=' 监控任务列表 ', font=('Microsoft YaHei', 10),
                            bg='#f0f0f0', padx=5, pady=5)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        lc = tk.Frame(left, bg='#f0f0f0')
        lc.pack(fill=tk.BOTH, expand=True)

        self.task_listbox = tk.Listbox(lc, font=('Microsoft YaHei', 9),
                                       selectmode=tk.SINGLE, height=10)
        sb = tk.Scrollbar(lc, orient=tk.VERTICAL, command=self.task_listbox.yview)
        self.task_listbox.configure(yscrollcommand=sb.set)
        self.task_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.task_listbox.bind('<<ListboxSelect>>', self._on_select)

        bf = tk.Frame(left, bg='#f0f0f0')
        bf.pack(fill=tk.X, pady=(5, 0))
        tk.Button(bf, text='删除选中', command=self._del, bg='#e74c3c', fg='white',
                  font=('Microsoft YaHei', 9), relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=2)
        tk.Button(bf, text='启用/禁用', command=self._toggle, bg='#f39c12', fg='white',
                  font=('Microsoft YaHei', 9), relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=2)

        # 状态面板
        status_frame = tk.LabelFrame(left, text=' 运行状态 ', font=('Microsoft YaHei', 10),
                                     bg='#f0f0f0', padx=5, pady=5)
        status_frame.pack(fill=tk.X, pady=(8, 0))

        self.status_labels = {}
        status_keys = [
            ('monitor', '监控状态'),
            ('current_chat', '当前聊天'),
            ('last_message', '最后检测消息'),
            ('last_match', '最后匹配任务'),
            ('last_trigger', '最后触发结果'),
            ('last_send', '最后发送结果'),
        ]
        for key, label in status_keys:
            row = tk.Frame(status_frame, bg='#f0f0f0')
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=label + ':', bg='#f0f0f0', font=('Microsoft YaHei', 8),
                     width=12, anchor='e').pack(side=tk.LEFT)
            val = tk.Label(row, text='-', bg='#f0f0f0', font=('Microsoft YaHei', 8, 'bold'),
                           fg='#555', anchor='w')
            val.pack(side=tk.LEFT, padx=(5, 0))
            self.status_labels[key] = val

        # 提示
        self.hint_label = tk.Label(status_frame, text='',
                                   bg='#f0f0f0', font=('Microsoft YaHei', 8), fg='#e67e22', wraplength=280)
        self.hint_label.pack(fill=tk.X, pady=(5, 0))

        # ---- 右侧: 编辑区 ----
        right = tk.LabelFrame(main, text=' 编辑任务 ', font=('Microsoft YaHei', 10),
                             bg='#f0f0f0', padx=5, pady=5)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(5, 0))

        tk.Label(right, text='聊天对象名 (完整匹配):', bg='#f0f0f0',
                 font=('Microsoft YaHei', 9), anchor='w').pack(fill=tk.X, pady=(5, 2))
        self.entry_chat = tk.Entry(right, font=('Microsoft YaHei', 10), relief=tk.SOLID, bd=1)
        self.entry_chat.pack(fill=tk.X, ipady=3)

        tk.Label(right, text='触发关键词 (逗号分隔):', bg='#f0f0f0',
                 font=('Microsoft YaHei', 9), anchor='w').pack(fill=tk.X, pady=(8, 2))
        self.entry_keywords = tk.Entry(right, font=('Microsoft YaHei', 10), relief=tk.SOLID, bd=1)
        self.entry_keywords.pack(fill=tk.X, ipady=3)

        tk.Label(right, text='自动回复内容:', bg='#f0f0f0',
                 font=('Microsoft YaHei', 9), anchor='w').pack(fill=tk.X, pady=(8, 2))
        self.text_reply = tk.Text(right, font=('Microsoft YaHei', 10), height=5,
                                  relief=tk.SOLID, bd=1, wrap=tk.WORD)
        self.text_reply.pack(fill=tk.X, pady=(0, 5))

        brf = tk.Frame(right, bg='#f0f0f0')
        brf.pack(fill=tk.X, pady=(5, 0))
        tk.Button(brf, text=' 添加/更新 ', command=self._add, bg='#27ae60', fg='white',
                  font=('Microsoft YaHei', 10, 'bold'), relief=tk.FLAT, padx=12, pady=4
                  ).pack(side=tk.LEFT, padx=2)
        tk.Button(brf, text=' 清空 ', command=self._clear_form, bg='#95a5a6', fg='white',
                  font=('Microsoft YaHei', 9), relief=tk.FLAT, padx=8, pady=4
                  ).pack(side=tk.LEFT, padx=2)

        tk.Label(right, text='轮询间隔 (秒):', bg='#f0f0f0',
                 font=('Microsoft YaHei', 9), anchor='w').pack(fill=tk.X, pady=(12, 2))
        inf = tk.Frame(right, bg='#f0f0f0')
        inf.pack(fill=tk.X)
        self.entry_interval = tk.Entry(inf, font=('Microsoft YaHei', 10), relief=tk.SOLID,
                                       bd=1, width=6)
        self.entry_interval.pack(side=tk.LEFT, ipady=3)
        self.entry_interval.insert(0, str(self.engine.poll_interval))
        tk.Button(inf, text='应用', command=self._set_interval, bg='#3498db', fg='white',
                  font=('Microsoft YaHei', 9), relief=tk.FLAT, padx=8, pady=2
                  ).pack(side=tk.LEFT, padx=5)

        # === 底部: 控制 + 日志 ===
        bottom = tk.Frame(self.root, bg='#f0f0f0')
        bottom.pack(fill=tk.X, padx=8, pady=(0, 5))

        ctrl = tk.Frame(bottom, bg='#f0f0f0')
        ctrl.pack(fill=tk.X, pady=(0, 5))

        self.btn_start = tk.Button(ctrl, text='  启动监控  ', command=self._start,
                                   bg='#27ae60', fg='white',
                                   font=('Microsoft YaHei', 11, 'bold'),
                                   relief=tk.FLAT, padx=18, pady=5)
        self.btn_start.pack(side=tk.LEFT, padx=3)

        self.btn_stop = tk.Button(ctrl, text='  停止监控  ', command=self._stop,
                                  bg='#e74c3c', fg='white',
                                  font=('Microsoft YaHei', 11, 'bold'),
                                  relief=tk.FLAT, padx=18, pady=5, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=3)

        self.monitor_led = tk.Label(ctrl, text='O 已停止',
                                    font=('Microsoft YaHei', 10, 'bold'),
                                    bg='#f0f0f0', fg='#e74c3c')
        self.monitor_led.pack(side=tk.LEFT, padx=18)

        # 日志
        logf = tk.LabelFrame(bottom, text=' 运行日志 ', font=('Microsoft YaHei', 9),
                            bg='#f0f0f0', padx=3, pady=3)
        logf.pack(fill=tk.BOTH, expand=True)

        self.log_text = self.scrolledtext.ScrolledText(
            logf, font=('Consolas', 9), height=10, relief=tk.SOLID, bd=1,
            wrap=tk.WORD, bg='#1e1e1e', fg='#d4d4d4')
        self.log_text.pack(fill=tk.BOTH, expand=True)
        for tag, color in [('trigger', '#f1c40f'), ('reply', '#2ecc71'),
                           ('error', '#e74c3c'), ('info', '#3498db'),
                           ('skip', '#95a5a6'), ('poll', '#8e44ad')]:
            self.log_text.tag_configure(tag, foreground=color)

    # ---- UI 事件 ----

    def _append_log(self, msg):
        if not self.log_text:
            return
        tags = []
        if '[KEYWORD]' in msg or '触发!' in msg:
            tags.append('trigger')
        if '[REPLY]' in msg or '[SEND]' in msg:
            tags.append('reply')
        if '失败' in msg or '异常' in msg:
            tags.append('error')
        if '[START]' in msg or '[STOP]' in msg or '[LOOP]' in msg:
            tags.append('info')
        if '[SKIP]' in msg:
            tags.append('skip')
        if '[POLL]' in msg or '[MATCH]' in msg or '[DEDUP]' in msg or '[CHAT]' in msg:
            tags.append('poll')
        if tags:
            for t in tags:
                self.log_text.insert(self.tk.END, msg + '\n', t)
        else:
            self.log_text.insert(self.tk.END, msg + '\n')
        self.log_text.see(self.tk.END)

    def _refresh_list(self):
        self.task_listbox.delete(0, self.tk.END)
        for n, c in self.engine.tasks.items():
            s = '[ON] ' if c.get('enabled', True) else '[OFF]'
            self.task_listbox.insert(self.tk.END,
                '{} {}  |  {}'.format(s, n, ','.join(c.get('keywords', []))))
        self._update_hint()

    def _update_status(self):
        """刷新状态面板"""
        st = self.engine.status
        labels = self.status_labels

        # 监控状态
        running = self.engine.monitor_running
        labels['monitor'].config(
            text='运行中' if running else '已停止',
            fg='#27ae60' if running else '#e74c3c'
        )
        self.monitor_led.config(
            text='O 运行中' if running else 'O 已停止',
            fg='#27ae60' if running else '#e74c3c'
        )

        labels['current_chat'].config(text=st.get('current_chat', '-') or '-')
        labels['last_message'].config(text=(st.get('last_message', '-') or '-')[:40])
        labels['last_match'].config(text=st.get('last_match_task', '-') or '-')
        labels['last_trigger'].config(text=st.get('last_trigger_result', '-') or '-')
        labels['last_send'].config(text=st.get('last_send_result', '-') or '-')

        self._update_hint()
        self.root.after(1000, self._update_status)

    def _update_hint(self):
        enabled = [n for n, c in self.engine.tasks.items() if c.get('enabled', True)]
        running = self.engine.monitor_running
        if enabled and not running:
            self.hint_label.config(
                text='任务已启用({}个)，但监控未启动，请点击"启动监控"。'.format(len(enabled)))
        elif not enabled:
            self.hint_label.config(text='还没有添加监控任务，请先在右侧填写并添加。')
        elif running:
            self.hint_label.config(text='监控运行中，正在监听 {} 个聊天。'.format(len(enabled)))
        else:
            self.hint_label.config(text='')

    def _on_select(self, event):
        sel = self.task_listbox.curselection()
        if not sel: return
        names = list(self.engine.tasks.keys())
        if sel[0] >= len(names): return
        n = names[sel[0]]
        c = self.engine.tasks[n]
        self._clear_form()
        self.entry_chat.insert(0, n)
        self.entry_keywords.insert(0, ','.join(c.get('keywords', [])))
        self.text_reply.insert('1.0', c.get('reply', ''))

    def _clear_form(self):
        self.entry_chat.delete(0, self.tk.END)
        self.entry_keywords.delete(0, self.tk.END)
        self.text_reply.delete('1.0', self.tk.END)

    def _add(self):
        c = self.entry_chat.get().strip()
        k = self.entry_keywords.get().strip()
        r = self.text_reply.get('1.0', self.tk.END).strip()
        if not c or not k or not r:
            self.messagebox.showwarning('提示', '请填写完整')
            return
        self.engine.add_task(c, k, r)
        self._refresh_list()
        self._clear_form()

    def _del(self):
        sel = self.task_listbox.curselection()
        if not sel: return
        names = list(self.engine.tasks.keys())
        if sel[0] >= len(names): return
        n = names[sel[0]]
        if self.messagebox.askyesno('确认删除', '确定要删除 [{}] 吗?'.format(n)):
            self.engine.remove_task(n)
            self._refresh_list()
            self._clear_form()

    def _toggle(self):
        sel = self.task_listbox.curselection()
        if not sel: return
        names = list(self.engine.tasks.keys())
        if sel[0] >= len(names): return
        n = names[sel[0]]
        c = self.engine.tasks[n]
        self.engine.toggle_task(n, not c.get('enabled', True))
        self._refresh_list()

    def _start(self):
        if self.engine.start():
            self.btn_start.config(state=self.tk.DISABLED)
            self.btn_stop.config(state=self.tk.NORMAL)
            self._update_status()

    def _stop(self):
        self.engine.stop()
        self.btn_start.config(state=self.tk.NORMAL)
        self.btn_stop.config(state=self.tk.DISABLED)
        self._update_status()

    def _set_interval(self):
        try:
            v = int(self.entry_interval.get().strip())
            if v < 1: raise ValueError
            self.engine.poll_interval = v
            self.engine.save_config()
            self.messagebox.showinfo('成功', '轮询间隔已设为 {} 秒'.format(v))
        except:
            self.messagebox.showwarning('错误', '请输入 1 以上的整数')

    def _pull_logs(self):
        if not self.log_text or not self.root: return
        try:
            while True:
                self._append_log(self.engine.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(300, self._pull_logs)

    def _console_mode(self):
        print('=' * 50)
        print('  wxauto 自动回复监控 - 控制台模式')
        print('=' * 50)
        self.engine.load_config()
        if self.engine.tasks:
            print('已加载 {} 个任务'.format(len(self.engine.tasks)))
            for n, c in self.engine.tasks.items():
                print('  {} {} -> {}'.format(
                    '[ON]' if c.get('enabled', True) else '[OFF]', n,
                    ','.join(c['keywords'])))
        print()
        print('命令: start | stop | quit | 聊天名|关键词|回复内容')
        def log_printer():
            while self.engine.monitor_running or not self.engine.log_queue.empty():
                try:
                    print(self.engine.log_queue.get(timeout=0.5))
                except queue.Empty:
                    if not self.engine.monitor_running: break
        while True:
            try: cmd = input('>> ').strip()
            except (EOFError, KeyboardInterrupt): break
            if not cmd: continue
            if cmd == 'quit': break
            if cmd == 'start':
                self.engine.start()
                threading.Thread(target=log_printer, daemon=True).start()
                continue
            if cmd == 'stop': self.engine.stop(); continue
            if cmd.startswith('del '):
                t = cmd[4:].strip()
                if t in self.engine.tasks:
                    self.engine.remove_task(t); print('已删除')
                else: print('未找到')
                continue
            if '|' in cmd:
                parts = [p.strip() for p in cmd.split('|')]
                if len(parts) >= 3:
                    self.engine.add_task(parts[0], parts[1].split(','),
                                         '|'.join(parts[2:])); print('已添加')
        self.engine.stop()
        print('已退出')


def main():
    engine = MonitorEngine(poll_interval=3)
    if '--console' in sys.argv:
        AutoReplyUI(engine)._console_mode()
    else:
        AutoReplyUI(engine).build_and_run()

if __name__ == '__main__':
    main()
