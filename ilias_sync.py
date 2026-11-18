#!/usr/bin/env python3
"""Personal, read-only, best-effort KIT ILIAS 10 offline synchronizer."""
from __future__ import annotations
import argparse
import hashlib
import getpass
import html as htmlmod
import json
import logging
import mimetypes
import os
import re
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

HOST = 'ilias.studium.kit.edu'
BASE = f'https://{HOST}'
DEFAULTS = [
    ('2971754','Advanced Bayesian Data Analysis', f'{BASE}/ilias.php?baseClass=ilrepositorygui&cmdNode=xu:mq&cmdClass=ilobjcoursegui&ref_id=2971754&item_ref_id=0'),
    ('3057194','Algorithms II', f'{BASE}/ilias.php?baseClass=ilrepositorygui&cmdClass=ilObjCourseGUI&ref_id=3057194'),
]
EXTS = {'.pdf','.doc','.docx','.ppt','.pptx','.xls','.xlsx','.zip','.rar','.7z','.txt','.csv','.tsv','.json','.xml','.md','.ipynb','.py','.r','.tex','.bib','.jpg','.jpeg','.png','.gif','.svg','.webp','.mp3','.mp4','.webm','.mov','.mkv','.odt','.ods','.odp','.epub'}
OBJECT_CLASSES = {'ilobjfoldergui','ilobjcoursegui','ilobjgroupgui','ilobjcontentpagegui','ilobjexercisegui','ilobjassignmentgui','ilobjforumgui','ilobjwikigui','ilobjfilegui','ilobjlearningmodulegui','ilobjbloggui','ilobjmediacastgui','ilobjopencastgui','ilobjweblinksgui','ilobjlinkresourcegui','ilobjtestgui','ilobjsurveygui','ilobjglossarygui','ilobjdatacollectiongui','ilobjbookingpoolgui','ilobjcalendargui','ilobjhtmllearningmodulegui'}
UNSAFE_CMD = re.compile(r'(?:delete|remove|unsubscribe|leave|edit|create|save|update|submit|upload|post|reply|new|add|copy|move|import|export|join|register|enrol|enroll|withdraw|confirm|cancel|startattempt|finishattempt|execute|perform|reset|rate|vote|mark|grade|send|mail|change|manage|administration)', re.I)
UNSAFE_PATH = re.compile(r'(?:logout|login\.php|administration|settings|ilias\.php/.*(?:edit|delete))', re.I)
INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
LOG = logging.getLogger('ilias-sync')


def stamp(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def safe(v, limit=95):
    v = re.sub(r'\s+', ' ', INVALID.sub('_', str(v))).strip(' .')
    if not v or v.upper() in {'CON','PRN','AUX','NUL','COM1','LPT1'}: v = '_'+(v or 'unnamed')
    return v[:limit].rstrip(' .') or 'unnamed'
def canonical(url):
    p=urlparse(url)
    return urlunparse((p.scheme,p.netloc,p.path,p.params,p.query,''))
def allowed(url):
    p=urlparse(url)
    return p.scheme=='https' and p.hostname==HOST and p.port in (None,443)
def ref_id(url):
    q=parse_qs(urlparse(url).query)
    return (q.get('ref_id') or [''])[0]
def course_id(url):
    if not allowed(url): raise ValueError('URL must be an HTTPS link to ilias.studium.kit.edu')
    ref=ref_id(url)
    if not ref.isdigit(): raise ValueError('Course URL must contain a numeric ref_id')
    return ref

def init_db(root):
    root.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(root/'index.sqlite3')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS courses(id TEXT PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS resources(course_id TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL,
        path TEXT NOT NULL, sha256 TEXT NOT NULL, title TEXT, source_page TEXT, checked_at TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(course_id,kind,url))''')
    db.execute('''CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, course_id TEXT NOT NULL,
        started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, pages INTEGER DEFAULT 0,
        saved INTEGER DEFAULT 0, unchanged INTEGER DEFAULT 0, errors INTEGER DEFAULT 0)''')
    db.commit()
    return db

def session_state_path(root):
    return root/'session-state.json'


def save_session(ctx,root):
    """Explicitly preserve session cookies, including cookies lost on browser close.

    WARNING: This file grants access to your account while the cookies are valid.
    """
    path=session_state_path(root)
    path.parent.mkdir(parents=True,exist_ok=True)
    # Use only cookies: the persistent browser profile already holds other site data.
    cookies=ctx.cookies()
    if not any(c.get('domain','').lstrip('.').endswith('ilias.studium.kit.edu') for c in cookies):
        raise RuntimeError('No ILIAS cookies exist after login; refusing to save an unauthenticated session.')
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'cookies':cookies},indent=2),encoding='utf-8')
    os.replace(tmp,path)
    if os.name=='nt':
        # Keep files private on Windows: restrict ACLs to the current user.
        import subprocess
        user=os.environ.get('USERNAME')
        if user:
            subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',f'{user}:F'],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)


def restore_session(ctx,root):
    path=session_state_path(root)
    if not path.exists(): return False
    data=json.loads(path.read_text(encoding='utf-8'))
    cookies=data.get('cookies',[])
    if cookies:
        ctx.add_cookies(cookies)
        return True
    return False


def profile(p,root,headless=True):
    ctx=p.chromium.launch_persistent_context(str(root/'browser-profile'),headless=headless,
            accept_downloads=True, viewport={'width':1365,'height':900})
    restore_session(ctx,root)
    return ctx

def login(root):
    with sync_playwright() as p:
        ctx=profile(p,root,False)
        page=ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE,wait_until='domcontentloaded',timeout=60000)
        print('Complete KIT SSO/MFA in the browser. Navigate to a course and verify you can see its content.')
        input('Press Enter here after you have logged in: ')
        probe_url=course_probe_url(root)
        verify_authenticated_course(page,probe_url)
        save_session(ctx,root)
        print('Authenticated session saved to:',session_state_path(root))
        ctx.close()

def kit_auth_host(url):
    """Only enter KIT account credentials on KIT-owned HTTPS hosts, never ILIAS guest login."""
    u=urlparse(url)
    return u.scheme=='https' and (u.hostname=='kit.edu' or (u.hostname or '').endswith('.kit.edu')) and u.hostname!=HOST


def enter_sso(page, username, password):
    """Fill the KIT identity provider login form, without writing the password to disk."""
    if not kit_auth_host(page.url):
        raise RuntimeError('Refusing to enter credentials: not a KIT SSO host: '+page.url)
    pw=page.locator('#password:visible').first
    if pw.count()!=1:
        raise RuntimeError('KIT SSO password field not found. Use `login` with visible browser.')
    username_selectors=('#username:visible', 'input[autocomplete="username"]:visible', 'input[name="j_username"]:visible',
        'input[name="username"]:visible', 'input[id="username"]:visible',
        'input[type="text"]:visible')
    ufield=None
    for sel in username_selectors:
        field=page.locator(sel).first
        if field.count():
            ufield=field
            break
    if ufield is None:
        raise RuntimeError('KIT SSO username field not found. Use `login` with visible browser.')
    ufield.fill(username)
    pw.fill(password)
    submit=page.locator('#sbmt:visible').first
    if submit.count():
        submit.click(timeout=15000)
    else:
        pw.press('Enter')


def _click_auth_option(page, patterns, selectors=()):
    """Click a visible login option (never an arbitrary account-management action)."""
    for selector in selectors:
        option=page.locator(selector).first
        try:
            if option.count() and option.is_visible(timeout=1500):
                option.click(timeout=10000)
                return True
        except Exception:
            continue
    for pattern in patterns:
        for role in ('link','button'):
            option=page.get_by_role(role,name=re.compile(pattern,re.I)).first
            try:
                if option.count() and option.is_visible(timeout=1500):
                    option.click(timeout=10000)
                    return True
            except Exception:
                continue
    return False


def navigate_to_kit_sso(page):
    """Start a fresh ILIAS SAML flow using verified KIT login element IDs."""
    login_url = BASE + '/login.php?client_id=produktiv&cmd=force_login&lang=de'
    page.goto(login_url, wait_until='domcontentloaded', timeout=60000)
    if kit_auth_host(page.url):
        return
    kit_button = page.locator('#button_shib_login')
    try:
        kit_button.wait_for(state='visible', timeout=20000)
    except Exception as exc:
        raise RuntimeError('KIT account button #button_shib_login not found on '+page.url) from exc
    kit_button.click(timeout=15000)
    try:
        page.wait_for_url(re.compile(r'^https://idp\.scc\.kit\.edu/'), timeout=30000)
    except Exception as exc:
        raise RuntimeError('KIT SSO redirect did not complete; current URL: '+page.url) from exc
    if not kit_auth_host(page.url):
        raise RuntimeError('Unexpected login host: '+page.url)
    try:
        page.locator('#password').wait_for(state='visible', timeout=20000)
    except Exception as exc:
        raise RuntimeError('KIT password field #password not found at '+page.url) from exc


def course_probe_url(root):
    db=init_db(root)
    try:
        row=db.execute('SELECT id FROM courses ORDER BY id LIMIT 1').fetchone()
        cid=row[0] if row else DEFAULTS[0][0]
        return course_view_url(cid)
    finally:
        db.close()


def course_view_url(cid):
    return f'{BASE}/ilias.php?baseClass=ilrepositorygui&cmdClass=ilObjCourseGUI&cmd=view&ref_id={cid}'


def terminal_login(root, sso_url=None):
    """Run Chromium invisibly; ask for password only in the terminal."""
    print('KIT account login (terminal mode). Password is not saved by this script.')
    username=input('KIT username: ').strip()
    if not username: raise ValueError('Username cannot be empty')
    password=getpass.getpass('KIT password (hidden): ')
    if not password: raise ValueError('Password cannot be empty')
    with sync_playwright() as p:
        ctx=profile(p,root,True)
        try:
            page=ctx.pages[0] if ctx.pages else ctx.new_page()
            if sso_url:
                if not kit_auth_host(sso_url):
                    raise ValueError('--sso-url must use HTTPS on a KIT-owned SSO host')
                page.goto(sso_url,wait_until='domcontentloaded',timeout=60000)
                # A raw execution URL may be stale or have no SAML request context.
                try:
                    page.locator('#password:visible').first.wait_for(timeout=5000)
                except Exception:
                    print('Direct SSO URL has no usable login form; starting a fresh ILIAS login flow.')
                    navigate_to_kit_sso(page)
            else:
                navigate_to_kit_sso(page)
            try:
                page.locator('#password:visible').first.wait_for(timeout=20000)
            except Exception:
                raise RuntimeError('KIT password field not found at '+page.url+'. Try `login` with visible browser.')
            enter_sso(page,username,password)
            password=None
            print('Credentials submitted. Complete MFA if KIT requests it.')
            deadline=time.monotonic()+180
            prompted=False
            while time.monotonic()<deadline:
                page.wait_for_timeout(1000)
                if urlparse(page.url).hostname==HOST and not auth_lost(page):
                    try:
                        verify_authenticated_course(page,course_probe_url(root))
                    except RuntimeError:
                        # A public info/help page is not proof of login.
                        continue
                    save_session(ctx,root)
                    print('Authenticated session verified and saved to:',session_state_path(root))
                    return
                if kit_auth_host(page.url):
                    codes=page.locator('input[autocomplete="one-time-code"]:visible, input[name="otp"]:visible, input[name="token"]:visible, input[id*="otp" i]:visible')
                    if codes.count() and not prompted:
                        code=getpass.getpass('MFA code (if your KIT login provides one): ')
                        if code:
                            codes.first.fill(code)
                            codes.first.press('Enter')
                        prompted=True
            raise RuntimeError('Login not completed within 3 minutes. Try `login` for interactive MFA.')
        finally:
            ctx.close()

def auth_lost(page):
    """Detect a guest/login page, even when the public course URL returns HTTP 200.

    ILIAS can display a public information screen with a ``force_login``
    link instead of redirecting to SSO. A successful HTTP response alone
    therefore never proves that authentication worked.
    """
    u=page.url.lower()
    if urlparse(u).hostname != HOST: return True
    if '/login.php' in u or 'cmd=force_login' in u: return True
    s=BeautifulSoup(page.content(),'html.parser')
    if s.select_one('form[action*="login"] input[type="password"], input[name="password"]'):
        return True
    # KIT ILIAS guest view shows an explicit Anmelden link pointing to
    # force_login (often with target=crs_<course ID>). Treat it as logged out.
    for a in s.select('a[href]'):
        href=unquote(a.get('href','')).lower()
        if '/login.php' in href and 'force_login' in href:
            return True
    return False


def verify_authenticated_course(page, url):
    """Navigate to a registered course and reject ILIAS's guest/info view."""
    response=page.goto(url,wait_until='domcontentloaded',timeout=60000)
    page.wait_for_timeout(500)
    if response is None or response.status >= 400:
        raise RuntimeError('Course verification request failed')
    if auth_lost(page):
        raise RuntimeError('KIT ILIAS is showing the guest/login page (Anmelden). '
                           'The browser session is not authenticated. Re-run login.')
    soup=BeautifulSoup(page.content(),'html.parser')
    # A public help/info page can return HTTP 200 and no login link. Require
    # strong evidence of the actual course content before accepting login.
    if not is_course_content(soup, page.url):
        raise RuntimeError('Could not verify the course Content view (possibly a public Help/Info page). '
                           'Login was not confirmed; try visible login if needed.')
    return True


def is_course_content(soup,url):
    if 'hilfe' == (soup.title.get_text(' ',strip=True).lower() if soup.title else ''):
        return False
    q=query(url)
    if q.get('cmdclass','').lower() != 'ilobjcoursegui':
        return False
    # Check for ILIAS course object folders/content links, as seen in KIT
    # course repositories. The check intentionally errs on the cautious side.
    for a in soup.select('a[href]'):
        href=urljoin(url,a['href'])
        target=goto_target(href)
        if re.fullmatch(r'(?:fold|file|frm|exc|pg|grp)_\d+(?:_download)?',target):
            return True
        cls=query(href).get('cmdclass','').lower()
        if cls in {'ilobjfoldergui','ilobjfilegui','ilobjforumgui','ilobjexercisegui'}:
            return True
    # Some ILIAS 10 course rows use path-style goto.php URLs.
    return bool(soup.select('a[href*="/goto.php/fold/"], a[href*="/goto.php/file/"], a[href*="/goto.php/frm/"]'))

def query(url):
    return {k.lower():v[0] for k,v in parse_qs(urlparse(url).query).items() if v}

def goto_target(url):
    """ILIAS has both query and path-style repository object permalinks."""
    p = urlparse(url)
    path = p.path.lower().rstrip('/')
    if path.endswith('/goto.php'):
        return query(url).get('target', '').lower()
    match = re.search(r'/goto\.php/(fold|file|crs|exc|grp|pg)/(\d+)(?:/(download))?$', path)
    if match:
        return f'{match.group(1)}_{match.group(2)}' + ('_download' if match.group(3) else '')
    return ''


def object_kind(url):
    """Only course-owned folder and file objects belong in a files-only crawl."""
    target = goto_target(url)
    if re.fullmatch(r'fold_\d+', target):
        return 'folder'
    if re.fullmatch(r'file_\d+(?:_download)?', target):
        return 'file'
    q = query(url)
    cls = q.get('cmdclass', '').lower()
    if q.get('ref_id', '').isdigit() and cls == 'ilobjfoldergui':
        return 'folder'
    if q.get('ref_id', '').isdigit() and cls == 'ilobjfilegui':
        return 'file'
    return None


def is_file_object(url):
    return object_kind(url) == 'file'


def download_variant(url):
    if not allowed(url) or not is_file_object(url):
        return None
    target = goto_target(url)
    if target.endswith('_download'):
        return canonical(url)
    match = re.fullmatch(r'file_(\d+)', target)
    if match:
        return f'{BASE}/goto.php?target=file_{match.group(1)}_download'
    rid = ref_id(url)
    if rid.isdigit():
        return f'{BASE}/goto.php?target=file_{rid}_download'
    return None


def is_download(url, anchor=None):
    q=query(url); path=urlparse(url).path.lower(); cls=q.get('cmdclass','').lower(); cmd=q.get('cmd','').lower()
    if anchor and anchor.has_attr('download'): return True
    if goto_target(url).startswith('file_') and goto_target(url).endswith('_download'): return True
    if path.endswith('/download.php') or '/sendfile' in path: return True
    if cls=='ilobjfilegui' and cmd in {'sendfile','download','downloadfile'}: return True
    if 'download' in cmd and not UNSAFE_CMD.search(cmd.replace('download','')): return True
    return Path(path).suffix.lower() in EXTS and not path.endswith('/ilias.php')


def is_safe_nav(url,root_course):
    """Files-only: never visit forums, user profiles, support or other courses."""
    if not allowed(url) or UNSAFE_PATH.search(urlparse(url).path):
        return False
    if UNSAFE_CMD.search(query(url).get('cmd','')):
        return False
    return object_kind(url) == 'folder'

def title_of(soup):
    for css in ('h1','h2.ilHeader','div.ilHeader h1','title'):
        x=soup.select_one(css)
        if x:
            t=x.get_text(' ',strip=True)
            if t: return safe(t)
    return 'Untitled page'

def main_content(soup):
    for bad in soup.select('script,style,noscript,header,footer,nav,.ilMainMenu,.ilToolbar,.ilMetaBar,.ilLeftNav,.ilBreadcrumbs'):
        bad.decompose()
    for css in ('#il_center_col','#ilContent','.ilContent','.ilc_page_cont','#mainspacekeeper','main'):
        el=soup.select_one(css)
        if el: return el
    return soup.body or soup

def page_text(element):
    # Text extraction is intentionally conservative: don't erase assignment links or instructions.
    parts=[]
    for line in element.get_text('\n',strip=True).splitlines():
        line=line.strip()
        if line and (not parts or parts[-1]!=line): parts.append(line)
    return '\n'.join(parts)+'\n'

def category(url,title,trail):
    s=(url+' '+title+' '+' '.join(trail)).lower()
    if any(x in s for x in ('assignment','exercisegui','übung','aufgabe','homework','abgabe','submission')): return 'assignments'
    if any(x in s for x in ('announcement','ankündigung','news','notice','mitteilung')): return 'announcements'
    if any(x in s for x in ('forum','thread','discussion')): return 'forums'
    if any(x in s for x in ('wiki','glossary')): return 'wiki'
    return 'pages'

def object_ref(url):
    q=query(url)
    return q.get('ref_id') or q.get('obj_id') or q.get('id') or ''

def folder_for(root,cid,name,trail):
    base=root/'courses'/safe(f'{name} [{cid}]')
    for component in trail[:12]: base=base/safe(component,55)
    return base

def resource_path(root,cid,name,kind,url,title,trail,ext):
    # Files belong directly in the ILIAS folder; page snapshots retain their type folder.
    base=folder_for(root,cid,name,trail)
    if kind != 'files':
        base=base/kind
    digest=hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]
    filename=f'{safe(title,78)}__{digest}{ext}'
    return base/filename

def save_bytes(db,root,cid,name,kind,url,title,trail,source,content,ext):
    h=hashlib.sha256(content).hexdigest()
    prior=db.execute('SELECT path,sha256 FROM resources WHERE course_id=? AND kind=? AND url=?',(cid,kind,url)).fetchone()
    expected = resource_path(root,cid,name,kind,url,title,trail,ext)
    path = Path(prior[0]) if prior else expected
    if kind == 'files' and prior:
        # A change of grouping (e.g. flat Slides -> Exercises/Slides) or the
        # retired /files/ directory requires a new output location.
        if path.parent != expected.parent:
            new_path = expected
            # Preserve the previously saved filename if it is a real file.
            if path.is_file():
                new_path = expected.parent / path.name
            course_root = folder_for(root,cid,name,())
            try:
                path.resolve().relative_to(course_root.resolve())
                new_path.resolve().relative_to(course_root.resolve())
            except ValueError:
                LOG.warning('Refusing to migrate a tracked file outside its course: %s',path)
            else:
                if new_path != path:
                    if new_path.exists():
                        # Do not overwrite an unrelated file; only dedupe if
                        # content matches the recorded SHA-256.
                        if new_path.is_file() and hashlib.sha256(new_path.read_bytes()).hexdigest() == prior[1]:
                            path = new_path
                        else:
                            new_path = new_path.with_name(new_path.stem + '__' + hashlib.sha1(url.encode()).hexdigest()[:8] + new_path.suffix)
                    if path != new_path and path.is_file() and not new_path.exists():
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(path, new_path)
                    path = new_path
                else:
                    path = new_path
    if prior and prior[1]==h and path.is_file():
        db.execute('UPDATE resources SET path=?,checked_at=? WHERE course_id=? AND kind=? AND url=?',
                   (str(path),stamp(),cid,kind,url))
        db.commit()
        return 'unchanged'
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.part')
    tmp.write_bytes(content)
    os.replace(tmp,path)
    db.execute('''INSERT INTO resources(course_id,kind,url,path,sha256,title,source_page,checked_at,size) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(course_id,kind,url) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,
        title=excluded.title,source_page=excluded.source_page,checked_at=excluded.checked_at,size=excluded.size''',
        (cid,kind,url,str(path),h,title,source,stamp(),len(content)))
    db.commit()
    LOG.info('SAVED %s',path.relative_to(root))
    return 'saved'

def html_snapshot(soup,title,url):
    content=main_content(soup)
    # Preserve readable local content and remote links, but remove scripts, forms, and active content.
    for el in content.select('form,iframe,object,embed,button,video,audio,svg,canvas'):
        el.decompose()
    for el in content.find_all(True):
        for attr in list(el.attrs):
            if attr.lower().startswith('on') or attr.lower() in {'srcset','integrity'}: del el[attr]
        for attr in ('href','src'):
            if el.has_attr(attr):
                val=el[attr]
                if isinstance(val,str):
                    dest=urljoin(url,val)
                    el[attr]=dest if urlparse(dest).scheme in {'http','https'} else '#'
    css='body{font:16px/1.55 system-ui,Arial;max-width:950px;margin:40px auto;padding:0 24px;color:#202020}img{max-width:100%}table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:6px}pre{white-space:pre-wrap}a{overflow-wrap:anywhere}'
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>'+htmlmod.escape(title)+'</title><style>'+css+'</style><body><p>Offline snapshot · <a href="'+htmlmod.escape(url,quote=True)+'">Original ILIAS page</a></p><hr>'+str(content)+'</body></html>').encode('utf-8')

def text_snapshot(soup):
    return page_text(main_content(soup)).encode('utf-8')

def filename_from_headers(headers,fallback,url):
    cd=headers.get('content-disposition','')
    x=re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;]+)",cd,re.I)
    if x: return safe(unquote(x.group(1).strip('"')))
    x=re.search(r'filename\s*=\s*"?([^";]+)',cd,re.I)
    if x: return safe(x.group(1))
    if Path(urlparse(url).path).suffix.lower() in EXTS: return safe(Path(urlparse(url).path).name)
    return safe(fallback)

def download_file(ctx,db,root,cid,name,url,label,trail,source,max_bytes):
    try:
        res=ctx.request.get(url,timeout=90000,max_redirects=8)
        if not res.ok: raise RuntimeError('HTTP '+str(res.status))
        if not allowed(res.url): raise RuntimeError('Download redirected off KIT ILIAS')
        ctype=res.headers.get('content-type','').lower()
        disposition=res.headers.get('content-disposition','').lower()
        if 'text/html' in ctype and 'attachment' not in disposition:
            raise RuntimeError('Returned a web page, not a downloadable file')
        clen=int(res.headers.get('content-length','0') or '0')
        if clen>max_bytes: raise RuntimeError('File exceeds max size')
        body=res.body()
        if len(body)>max_bytes: raise RuntimeError('File exceeds max size')
        fname=filename_from_headers(res.headers,label,res.url)
        ext=Path(fname).suffix
        if not ext: ext=mimetypes.guess_extension(ctype.split(';')[0]) or '.bin'
        return save_bytes(db,root,cid,name,'files',url,Path(fname).stem,trail,source,body,ext)
    except Exception as e:
        LOG.warning('DOWNLOAD skipped %s (%s)',url,e)
        return 'error'

def link_label(a):
    text=a.get_text(' ',strip=True) or a.get('title') or a.get('aria-label')
    if not text:
        img=a.select_one('img[alt]')
        if img: text=img.get('alt')
    return safe(text or 'Resource')

def folder_identity(url):
    """Normalize ILIAS folder permalinks, query links and content-view URLs."""
    target = goto_target(url)
    match = re.fullmatch(r'fold_(\d+)', target)
    if match:
        return match.group(1)
    q = query(url)
    if q.get('cmdclass', '').lower() == 'ilobjfoldergui' and q.get('ref_id', '').isdigit():
        return q['ref_id']
    return None


def repository_content(soup):
    """Look in the actual repository listing before falling back to the document."""
    for selector in (
        '#il_center_col', '#ilContent', '.ilContent', '#mainspacekeeper',
        'main', '[role="main"]',
    ):
        tag = soup.select_one(selector)
        if tag and tag.select('a[href*="goto.php/fold/"],a[href*="target=fold_"],a[href*="ilObjFolderGUI" i]'):
            return tag
    return soup.body or soup


def is_chrome_anchor(anchor):
    """Exclude breadcrumb, global navigation and other non-repository links."""
    for parent in anchor.parents:
        if parent.name in ('nav', 'header', 'footer'):
            return True
        classes = ' '.join(parent.get('class', [])).lower()
        ident = str(parent.get('id', '')).lower()
        if any(term in classes + ' ' + ident for term in (
            'breadcrumb', 'il-main-menu', 'ilmainmenu', 'ilmetabar',
            'il-mainbar', 'ilmainbar', 'iltoolbar', 'il-toolbar',
        )):
            return True
        if parent.name in ('body', 'html'):
            break
    return False


def section_for_anchor(a, content=None):
    """Resolve a virtual grouping heading without leaking across repository sections.

    Search local repository item-group / panel wrappers first; otherwise choose
    the closest preceding heading *inside the content area*. Do not use a panel
    title belonging to a different sibling group.
    """
    def heading(tag):
        if not getattr(tag, 'name', None):
            return False
        classes = ' '.join(tag.get('class', [])).lower()
        return (tag.name in {'h2', 'h3', 'h4', 'h5'} or
                ('heading' in classes or 'group-title' in classes or 'section-title' in classes)
                and tag.name not in {'a', 'button'})

    def clean(tag):
        value = tag.get_text(' ', strip=True)
        if not value or len(value) > 65 or value.casefold() in {
            'content', 'contents', 'inhalt', 'repository', 'magazin',
            'course content', 'kursinhalt', 'items', 'objects',
        }:
            return None
        return safe(value, 55)

    boundary = content or a.find_parent('main') or a.find_parent('body')
    for container in a.parents:
        if container is boundary or container.name in {'body', 'html'}:
            break
        cls = ' '.join(container.get('class', [])).lower()
        if not (container.name == 'section' or any(token in cls for token in
                    ('item-group', 'panel', 'accordion', 'repository-group'))):
            continue
        # Heading must precede the link in DOM order within this container.
        for h in reversed(container.find_all(heading)):
            if h in a.find_all_previous(heading) and (v := clean(h)):
                return v

    # Walk backward only as far as the repository content ancestor; stop at
    # the nearest heading. Unlike the old code, never fall back to a heading
    # on an unrelated course-wide navigation panel.
    if boundary:
        headings = set(id(x) for x in boundary.find_all(heading))
        for h in a.find_all_previous(heading):
            if id(h) not in headings:
                continue
            if (v := clean(h)):
                return v
    return None

def collect_links(soup,page_url,cid,with_sections=False):
    """Return ONLY folder pages and actual file downloads; never shell navigation.

    The files-only crawler starts at one course and discovers its nested folder
    objects. No forum, profile, unrelated category or support page is enqueued.
    """
    result=[]
    content=repository_content(soup)
    for a in content.select('a[href]'):
        if is_chrome_anchor(a):
            continue
        raw=a.get('href','').strip()
        if not raw or raw.startswith(('#','javascript:','mailto:','tel:','data:')):
            continue
        url=canonical(urljoin(page_url,raw))
        if not allowed(url):
            continue
        label=link_label(a)
        kind=object_kind(url)
        section=section_for_anchor(a, content) if with_sections else None
        if kind=='folder' and is_safe_nav(url,cid):
            result.append((url,label,False,section) if with_sections else (url,label,False))
        elif kind=='file':
            direct=download_variant(url)
            if direct:
                result.append((direct,label,True,section) if with_sections else (direct,label,True))
        elif is_download(url,a) and not UNSAFE_CMD.search(query(url).get('cmd','')):
            # A real direct file link (not a forum attachment or arbitrary page).
            if Path(urlparse(url).path).suffix.lower() in EXTS:
                result.append((url,label,True,section) if with_sections else (url,label,True))
    return list(dict.fromkeys(result))

def describe_links(soup,page_url,cid):
    """Safe diagnostic: summarize anchor URLs, dropping unknown query fields.

    Never output full URLs from authenticated pages: they may contain session
    tokens or signed download parameters.
    """
    results=[]
    for a in soup.select('a[href]'):
        raw=a.get('href','').strip()
        if not raw or raw.startswith(('javascript:','mailto:','tel:','data:','#')): continue
        url=urljoin(page_url,raw)
        if not allowed(url): continue
        parsed=urlparse(url)
        q=query(url)
        visible={key:q[key] for key in ('baseclass','cmdclass','cmd','ref_id') if key in q}
        if re.fullmatch(r'(?:fold|crs|grp|file|exc|pg|frm|wiki|lm|webr|cont|cat)_\d+(?:_download)?',q.get('target','')):
            visible['target']=q['target']
        # Do not expose names, user data, dynamic query values or raw onclicks.
        label=link_label(a)
        safe_label=label[:65] if len(label)<66 else label[:62]+'...'
        classification=('DOWNLOAD' if is_download(url,a) else
                        'NAV' if is_safe_nav(url,cid) else 'ignored')
        results.append((classification,safe_label,parsed.path,visible))
    return results

def trail_for(parent_trail, label, url, is_root):
    if is_root:
        return ()
    # Never append the same folder twice. Breadcrumb/self-links must not
    # result in Slides/Slides, and folder pages already carry their own trail.
    component = safe(label, 55)
    if parent_trail and parent_trail[-1].casefold() == component.casefold():
        return parent_trail
    return (parent_trail + (component,))[:12]

def sync_course(ctx,db,root,cid,name,url,max_pages,delay,max_mb,inspect_links=False,save_pages=False):
    started=stamp()
    run=db.execute('INSERT INTO runs(course_id,started_at,status) VALUES(?,?,?)',(cid,started,'running')).lastrowid; db.commit()
    queue=deque([(canonical(url),(),name,True)])
    seen=set(); seen_folders=set(); files={}; counts={'pages':0,'saved':0,'unchanged':0,'errors':0}
    page=ctx.new_page()
    try:
        while queue and counts['pages']<max_pages:
            current,trail,label,is_root=queue.popleft()
            fid=folder_identity(current)
            if current in seen or (fid and fid in seen_folders):
                continue
            seen.add(current)
            if fid:
                seen_folders.add(fid)
            try:
                response=page.goto(current,wait_until='domcontentloaded',timeout=60000)
                page.wait_for_timeout(400)
                if auth_lost(page): raise RuntimeError('Authentication expired or guest view returned (Anmelden). Run `login` again.')
                if response and response.status>=400: raise RuntimeError(f'HTTP {response.status}')
                if not allowed(page.url): raise RuntimeError('Redirected outside KIT ILIAS')
                rendered=page.content()
                soup=BeautifulSoup(rendered,'html.parser')
                if not soup.select_one('body'):raise RuntimeError('Empty or invalid page')
                title=title_of(soup)
                if 'login' in title.lower() and soup.select_one('input[type=password]'):
                    raise RuntimeError('Login page returned instead of course content')
                if save_pages:
                    kind=category(current,title,trail)
                    h=html_snapshot(BeautifulSoup(rendered,'html.parser'),title,page.url)
                    t=text_snapshot(BeautifulSoup(rendered,'html.parser'))
                    for ext,blob in (('.html',h),('.txt',t)):
                        outcome=save_bytes(db,root,cid,name,kind+ext,current,title,trail,current,blob,ext)
                        counts['saved' if outcome=='saved' else 'unchanged']+=1
                counts['pages']+=1
                links=collect_links(soup,page.url,cid,with_sections=is_root)
                if inspect_links:
                    summary=describe_links(soup,page.url,cid)
                    print(f'INSPECT page={counts["pages"]} title={title!r} anchors={len(summary)} accepted={len(links)}')
                    for row_kind,text_label,path,fields in summary[:100]:
                        if row_kind in {'NAV','DOWNLOAD'}:
                            print(f'  {row_kind:<8} {text_label[:45]:<45} {path} {fields}')
                if inspect_links and is_root:
                    for dest,entry_name,is_file,section in links:
                        print(f'  GROUP    {(section or "(course root)") + " / " + entry_name} [{"file" if is_file else "folder"}]')
                LOG.info('FOLDER %s | found %d items', ' / '.join(trail) if trail else name,len(links))
                for item in links:
                    if is_root:
                        dest,link_title,isfile,section=item
                        target_trail=trail+(section,) if section else trail
                    else:
                        dest,link_title,isfile=item
                        target_trail=trail
                    if isfile:
                        files.setdefault(dest,(link_title,target_trail,current))
                    elif dest not in seen and len(queue)<max_pages*6:
                        child_id=folder_identity(dest)
                        if child_id and (child_id in seen_folders or child_id == folder_identity(current)):
                            continue
                        # Avoid enqueuing the same folder under two different
                        # URL spellings and therefore creating nested duplicates.
                        if child_id and any(folder_identity(u) == child_id for u, *_ in queue):
                            continue
                        next_trail=trail_for(target_trail,link_title,dest,False)
                        queue.append((dest,next_trail,link_title,False))
                LOG.info('PAGE %s | %s | links %d',counts['pages'],title,len(links))
            except RuntimeError as ex:
                counts['errors']+=1; LOG.warning('PAGE %s (%s)',current,ex)
                if 'Authentication expired' in str(ex): raise
            except Exception as ex:
                counts['errors']+=1; LOG.warning('PAGE failed %s (%s)',current,ex)
            time.sleep(delay)
        for i,(source_url,(label,trail,from_page)) in enumerate(files.items(),1):
            LOG.info('FILE %d/%d %s',i,len(files),label)
            outcome=download_file(ctx,db,root,cid,name,source_url,label,trail,from_page,max_mb*1024*1024)
            counts['saved' if outcome=='saved' else 'unchanged' if outcome=='unchanged' else 'errors']+=1
            time.sleep(delay)
        status='partial' if counts['errors'] or queue else 'complete'
        return status,counts,len(queue)
    except Exception:
        db.execute('UPDATE runs SET finished_at=?,status=?,pages=?,saved=?,unchanged=?,errors=? WHERE id=?',
                   (stamp(),'failed',counts['pages'],counts['saved'],counts['unchanged'],counts['errors']+1,run));db.commit()
        raise
    finally:
        if 'status' in locals():
            db.execute('UPDATE runs SET finished_at=?,status=?,pages=?,saved=?,unchanged=?,errors=? WHERE id=?',
                       (stamp(),status,counts['pages'],counts['saved'],counts['unchanged'],counts['errors'],run));db.commit()
        page.close()

def make_index(db,root,cid,name):
    base=folder_for(root,cid,name,())
    base.mkdir(parents=True,exist_ok=True)
    rows=db.execute('SELECT kind,path,title,url,checked_at FROM resources WHERE course_id=? ORDER BY kind,title',(cid,)).fetchall()
    lines=['<!doctype html><html><meta charset="utf-8"><title>'+htmlmod.escape(name)+'</title><body><h1>'+htmlmod.escape(name)+'</h1><p>Local offline index; content updates are best effort.</p><ul>']
    for kind,path,title,url,checked in rows:
        target=Path(path)
        if not target.exists():continue
        relative=os.path.relpath(target,base).replace(os.sep,'/')
        lines.append('<li><a href="'+htmlmod.escape(relative,quote=True)+'">'+htmlmod.escape(f'[{kind}] {title or target.stem}')+'</a> · <a href="'+htmlmod.escape(url,quote=True)+'">online</a></li>')
    lines.append('</ul></body></html>')
    (base/'INDEX.html').write_text('\n'.join(lines),encoding='utf-8')

def main():
    parser=argparse.ArgumentParser(description='Windows-compatible, read-only KIT ILIAS offline sync')
    parser.add_argument('--data-dir',type=Path,default=Path('ilias_data'),help='Persistent data folder (default: ./ilias_data)')
    parser.add_argument('--verbose',action='store_true')
    commands=parser.add_subparsers(dest='command',required=True)
    login_parser=commands.add_parser('login',help='Log in via KIT SSO in browser or terminal')
    login_parser.add_argument('--terminal',action='store_true',help='Prompt for KIT username/password in PowerShell, using headless Chromium')
    login_parser.add_argument('--sso-url',help='Optional direct KIT SSO URL (may expire); use with --terminal')
    commands.add_parser('init',help='Add the two example courses')
    add=commands.add_parser('add',help='Add/update a course URL'); add.add_argument('url'); add.add_argument('--name',required=True)
    rm=commands.add_parser('remove',help='Stop syncing a course; never deletes files'); rm.add_argument('course_id')
    commands.add_parser('list',help='List subscribed courses')
    commands.add_parser('status',help='Show sync history')
    commands.add_parser('check-auth',help='Verify saved browser session can access a course (no downloads)')
    sync=commands.add_parser('sync',help='Synchronize files from course folders recursively (skip forums)')
    sync.add_argument('--course',help='Restrict to one course ref_id')
    sync.add_argument('--max-pages',type=int,default=250)
    sync.add_argument('--delay',type=float,default=0.7,help='Seconds between requests (default 0.7)')
    sync.add_argument('--max-file-mb',type=int,default=250)
    sync.add_argument('--show-browser',action='store_true',help='Watch traversal and troubleshoot')
    sync.add_argument('--inspect-links',action='store_true',help='Print sanitized link-discovery report for each page')
    sync.add_argument('--save-pages',action='store_true',help='Also save HTML/text snapshots (off by default)')
    args=parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,format='%(levelname)s %(message)s')
    root=args.data_dir.expanduser().resolve()
    db=init_db(root)
    try:
        if args.command=='login':
            if args.terminal: terminal_login(root,args.sso_url)
            else: login(root)
        elif args.command=='init':
            for cid,name,url in DEFAULTS:
                db.execute('INSERT INTO courses VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url',(cid,name,url))
                print('Added',name,cid)
            db.commit()
        elif args.command=='add':
            cid=course_id(args.url)
            db.execute('INSERT INTO courses VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url',(cid,args.name,args.url));db.commit()
            print('Added/updated',args.name,'['+cid+']')
        elif args.command=='remove':
            db.execute('DELETE FROM courses WHERE id=?',(args.course_id,));db.commit()
            print('Removed subscription; files retained:',args.course_id)
        elif args.command=='list':
            for cid,name,url in db.execute('SELECT id,name,url FROM courses ORDER BY name'):
                print(f'{cid}  {name}\n    {url}')
        elif args.command=='status':
            for cid,name in db.execute('SELECT id,name FROM courses ORDER BY name'):
                n=db.execute('SELECT COUNT(*) FROM resources WHERE course_id=?',(cid,)).fetchone()[0]
                last=db.execute('SELECT started_at,status,pages,saved,errors FROM runs WHERE course_id=? ORDER BY id DESC LIMIT 1',(cid,)).fetchone()
                print(f'{name} [{cid}]  {n} stored resources; last run: {last or "never"}')
        elif args.command=='check-auth':
            course=db.execute('SELECT url FROM courses ORDER BY name LIMIT 1').fetchone()
            check_url=course_view_url(course_id(course[0])) if course else course_probe_url(root)
            with sync_playwright() as p:
                ctx=profile(p,root,True)
                try:
                    page=ctx.new_page()
                    try:
                        verify_authenticated_course(page,check_url)
                        print('AUTH OK: actual course Content view accessible; saved cookies restored.')
                    finally:
                        page.close()
                finally:
                    ctx.close()
        elif args.command=='sync':
            if args.max_pages<1 or args.delay<0.2 or args.max_file_mb<1: raise ValueError('Use max-pages >=1, delay >=0.2, max-file-mb >=1')
            courses=db.execute('SELECT id,name,url FROM courses ORDER BY name').fetchall()
            if args.course: courses=[c for c in courses if c[0]==args.course]
            if not courses: raise ValueError('No matching courses. Use `init` or `add`.')
            with sync_playwright() as p:
                ctx=profile(p,root,not args.show_browser)
                try:
                    for cid,name,url in courses:
                        print(f'\nSYNC {name} [{cid}]')
                        status,counts,pending=sync_course(ctx,db,root,cid,name,url,args.max_pages,args.delay,args.max_file_mb,args.inspect_links,args.save_pages)
                        make_index(db,root,cid,name)
                        print(f'{status.upper()}: {counts}; {pending} pages beyond crawl limit')
                finally:ctx.close()
    finally:db.close()

if __name__=='__main__':
    try:main()
    except (ValueError,RuntimeError,KeyboardInterrupt) as e:
        print('ERROR:',e,file=sys.stderr);sys.exit(1)
