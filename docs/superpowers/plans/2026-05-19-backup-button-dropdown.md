# Backup Button Dropdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the "Download backup" button out of the nav bar and into a dropdown that opens when the user clicks their username.

**Architecture:** Pure front-end change in `dashboard.html`. Add CSS for the dropdown, add a wrapper `<div>` around the username trigger, rewrite `updateAuthUI()` to render the dropdown markup, and add a `toggleUserMenu()` JS function with an outside-click handler. No Python, no API, no new files.

**Tech Stack:** Vanilla HTML/CSS/JS inside `dashboard.html`

---

### Task 1: Add dropdown CSS

**Files:**
- Modify: `dashboard.html` — CSS block (around line 62–110)

- [ ] **Step 1: Add the following CSS after the `.btn-ghost:hover` rule (line 73)**

```css
.user-menu{position:relative;display:inline-flex;align-items:center}
.user-menu-trigger{background:none;border:none;cursor:pointer;color:var(--muted);font-family:'DM Mono',monospace;font-size:11px;padding:0;display:inline-flex;align-items:center;gap:4px}
.user-menu-trigger:hover{color:var(--text)}
.user-menu-chevron{font-size:9px;opacity:.6}
.user-dropdown{display:none;position:absolute;top:calc(100% + 6px);right:0;background:var(--surface);border:1px solid var(--border);border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,.12);min-width:150px;z-index:100;overflow:hidden}
.user-dropdown.open{display:block}
.user-dropdown-item{display:block;width:100%;text-align:left;background:none;border:none;padding:9px 14px;font-family:'DM Sans',sans-serif;font-size:12px;color:var(--text);cursor:pointer;white-space:nowrap}
.user-dropdown-item:hover{background:var(--subtle)}
```

- [ ] **Step 2: Verify no typos by checking the page still loads**

```bash
python3 -c "
import re, sys
html = open('/home/mgipson/netwatch/dashboard.html').read()
if '.user-dropdown' in html:
    print('CSS found OK')
else:
    print('CSS missing'); sys.exit(1)
"
```
Expected: `CSS found OK`

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: add user menu dropdown CSS"
```

---

### Task 2: Rewrite `updateAuthUI()` to render the dropdown

**Files:**
- Modify: `dashboard.html` — `updateAuthUI()` function (around line 3927–3940)

- [ ] **Step 1: Replace the `updateAuthUI` function body**

Find and replace the entire function (lines 3927–3940):

```javascript
function updateAuthUI(){
  const navAuth = document.getElementById('nav-auth');
  if(!navAuth) return;
  if(_authState.logged_in){
    const label = escapeHtml(_authState.username) + (_authState.admin ? ' <span style="opacity:.5">(admin)</span>' : '');
    const backupItem = _authState.admin
      ? '<button class="user-dropdown-item" onclick="toggleUserMenu();downloadBackup()">Download backup</button>'
      : '';
    navAuth.innerHTML =
      '<div class="user-menu" id="user-menu">'
      + '<button class="user-menu-trigger" onclick="toggleUserMenu()" aria-haspopup="true" aria-expanded="false" id="user-menu-trigger">'
      + label
      + '<span class="user-menu-chevron">▾</span>'
      + '</button>'
      + '<div class="user-dropdown" id="user-dropdown">'
      + backupItem
      + '<button class="user-dropdown-item" onclick="toggleUserMenu();logout()">Log out</button>'
      + '</div>'
      + '</div>';
  } else {
    navAuth.innerHTML = '<button class="btn" onclick="openLogin()" style="font-size:12px">Log in</button>';
  }
}
```

- [ ] **Step 2: Add `toggleUserMenu()` immediately after `updateAuthUI()`**

Insert after the closing `}` of `updateAuthUI()`:

```javascript
function toggleUserMenu(){
  const dropdown = document.getElementById('user-dropdown');
  const trigger  = document.getElementById('user-menu-trigger');
  if(!dropdown) return;
  const opening = !dropdown.classList.contains('open');
  dropdown.classList.toggle('open', opening);
  if(trigger) trigger.setAttribute('aria-expanded', opening ? 'true' : 'false');
  if(opening){
    function outsideClick(e){
      const menu = document.getElementById('user-menu');
      if(menu && !menu.contains(e.target)){
        dropdown.classList.remove('open');
        if(trigger) trigger.setAttribute('aria-expanded','false');
        document.removeEventListener('click', outsideClick, true);
      }
    }
    // Use capture so the handler fires before any other click logic
    document.addEventListener('click', outsideClick, true);
  }
}
```

- [ ] **Step 3: Verify the old standalone backup button reference is gone**

```bash
python3 -c "
html = open('/home/mgipson/netwatch/dashboard.html').read()
if 'Download backup</button>\\',' in html or 'btn btn-ghost.*downloadBackup' in __import__('re').search('.*', html, re.S).group():
    pass
import re
m = re.search(r'btn btn-ghost.*?downloadBackup', html)
if m:
    print('OLD button still present:', m.group()); __import__('sys').exit(1)
else:
    print('Old button removed OK')
"
```
Expected: `Old button removed OK`

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "feat: move backup button into username dropdown"
```

---

### Task 3: Manual browser verification

**Files:** none — verification only

- [ ] **Step 1: Restart the service to pick up changes**

```bash
sudo systemctl restart netwatch && systemctl status netwatch --no-pager | grep Active
```
Expected: `Active: active (running)`

- [ ] **Step 2: Open the dashboard and log in as admin. Verify:**
  - Username appears in nav with a `▾` chevron
  - Clicking username opens a dropdown with "Download backup" and "Log out"
  - Clicking outside closes the dropdown
  - "Download backup" triggers the file download and closes the dropdown
  - "Log out" logs out and closes the dropdown

- [ ] **Step 3: Log in as a non-admin user. Verify:**
  - Dropdown contains only "Log out" (no backup item)

- [ ] **Step 4: Commit if any fixups were needed, otherwise done**
