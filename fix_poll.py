"""
Fix: replace UIA message reading with Ctrl+A Ctrl+C clipboard approach
"""
path = r'C:\Users\86137\Desktop\wxauto-main\auto_reply.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Old text block (from "_poll_all_rules" method)
old = '''            # 2. 取最新一条 incoming (后台检测, 不切窗口)
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
                continue'''

new = '''            # 2. 读取消息: 点击消息区 > Ctrl+A > Ctrl+C 从剪贴板获取
            latest_text = ""
            for _retry in range(3):
                try:
                    # click message area (middle of ChatMessagePage)
                    st = FreshNav._nav_right_panel()
                    if st:
                        cp = None
                        try:
                            for c in st.GetChildren():
                                if c.ClassName == "mmui::ChatMessagePage":
                                    cp = c; break
                        except: pass
                        if cp:
                            rr = cp.BoundingRectangle
                            # click center of message area (above input box)
                            mouse_click((rr.left+rr.right)//2, (rr.top+rr.bottom)//2 - 80)
                            time.sleep(0.08)
                            # select all and copy
                            uia.SendKeys("{Ctrl}a", waitTime=0.05)
                            time.sleep(0.1)
                            uia.SendKeys("{Ctrl}c", waitTime=0.05)
                            time.sleep(0.2)
                            latest_text = pyperclip.paste()
                            if latest_text.strip():
                                break
                except: pass
                time.sleep(0.3)

            if not latest_text or not latest_text.strip():
                continue

            # Extract the last meaningful line (ignore timestamps/date headers)
            lines = [l.strip() for l in latest_text.split(chr(10)) if l.strip()]
            # Filter out lines that look like timestamps
            filtered = []
            for l in lines:
                if len(l) > 3 and not (len(l) < 8 and all(c in '0123456789: -' for c in l)):
                    if '20' == l[:2] and '-' in l: continue  # "2025-07-01 17:04:37"
                    filtered.append(l)
            if not filtered:
                filtered = lines  # fallback: use all lines
            latest_content = filtered[-1]  # last meaningful line

            if latest_content is None or not latest_content.strip():
                self.log('[POLL] 聊天: {} 无消息'.format(chat))
                continue'''

content = content.replace(old, new)

# Fix the rest of the message processing
# "full = latest.name" -> "full = latest_content"
content = content.replace('full = latest.name', 'full = latest_content')

# Fix "lat" references
content = content.replace('latest.name', 'latest_content')
content = content.replace('msg.name', 'latest_content')
content = content.replace('msg.content_hash', 'hashlib.md5(latest_content.encode()).hexdigest()[:16]')
content = content.replace('msg.runtime_id', "''")

# Fix variable name conflict
content = content.replace('content.strip()', 'latest_content.strip()')

# Fix _process_rule references
old_rule_set = '''            full = latest_content
            sender = ''
            content = full
            if chr(10) in full and len(full.split(chr(10))[0]) < 30:'''

new_rule_set = '''            full = latest_content
            sender = ""
            if chr(10) in full and len(full.split(chr(10))[0]) < 30:'''
content = content.replace(old_rule_set, new_rule_set)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print('OK - polling replaced with clipboard approach')
