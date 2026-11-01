"""Bounded page execution in a fresh browser; all HTTP uses the public broker.

No existing profile, browser login, arbitrary evaluate command or host file URL.
The page's executable content remains untrusted, including rendered text.
"""
from __future__ import annotations

import asyncio
import importlib.util
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import quote, unquote, urlsplit, urljoin

from .capabilities import confined
from .models import OperationResult, PolicyError, now
from .providers import _validate_url, ModelGateway, WebAcquisitionError
from .store import redact

LOCAL_ORIGIN = 'https://oif-workspace.invalid'
MAX_REQUESTS = 160
MAX_TOTAL_BYTES = 32_000_000
MAX_BODY_BYTES = 8_000_000
STORAGE_LIMIT = 64


def browser_channels():
    candidates = []
    if os.name == 'nt':
        for env, tail, channel in (
            ('PROGRAMFILES(X86)', 'Microsoft/Edge/Application/msedge.exe', 'msedge'),
            ('PROGRAMFILES', 'Microsoft/Edge/Application/msedge.exe', 'msedge'),
            ('PROGRAMFILES', 'Google/Chrome/Application/chrome.exe', 'chrome'),
            ('LOCALAPPDATA', 'Google/Chrome/Application/chrome.exe', 'chrome')):
            if os.environ.get(env) and (Path(os.environ[env]) / tail).is_file() and channel not in candidates:
                candidates.append(channel)
    return [*candidates, None]  # Installed Playwright Chromium on other platforms.


def availability():
    return dict(driver_available=importlib.util.find_spec('playwright') is not None,
                preferred_channel=browser_channels()[0] or 'chromium',
                scope='Fresh isolated browser: public HTTPS or task HTML; JS, rendered DOM, read requests and storage. Browser installation is verified at launch.')


def arguments(args):
    if set(args) - {'url','path','wait_ms','timeout_seconds','refresh_reason'} or bool(args.get('url')) == bool(args.get('path')):
        raise PolicyError('browser_inspect needs exactly one public HTTPS url or task-relative HTML path')
    wait, timeout = args.get('wait_ms', 1500), args.get('timeout_seconds', 30)
    if type(wait) is not int or not 0 <= wait <= 10000 or type(timeout) not in (int,float) or not 5 <= timeout <= 60:
        raise PolicyError('Browser wait_ms must be 0..10000; timeout_seconds must be 5..60')
    if 'refresh_reason' in args and (not isinstance(args['refresh_reason'],str) or not args['refresh_reason'].strip() or len(args['refresh_reason'])>1000):
        raise PolicyError('A fresh observation needs a short refresh_reason explaining what must be observed again')
    return wait, timeout


def storage_value(key, value):
    # New anonymous sessions can still issue session credentials. Expose names
    # and attributes, not authentication values in model/UI output.
    if re.search(r'(?i)(token|secret|password|authorization|session|auth|jwt|csrf|xsrf)', key):
        return '[hidden session value]'
    return redact(str(value))[:2000]


class BrowserObserver:
    def __init__(self, store, web):
        self.store, self.web = store, web

    def recover(self, operation):
        saved = self.store.record_get('browser_observation', operation.id)
        if saved and saved.get('result'):
            return OperationResult.model_validate(saved['result'])
        return OperationResult(operation_id=operation.id, status='unknown', effect='unknown',
            stderr='Browser observation was interrupted before its result was saved; no page was reopened.',
            data={'partial_observation': (saved or {}).get('observation'), 'automatic_replay': False})

    async def inspect(self, task, operation, before_request):
        arguments(operation.args)
        before_request()
        rows=self.store.operation_headers(task['id'],max(0,self.store.operation_count(task['id'])-2),2)
        prior=next((r for r in reversed(rows) if r['operation']['id']!=operation.id),None)
        if prior and 'refresh_reason' not in operation.args:
            prior=self.store.get_operation(prior['operation']['id'])
            result=prior.get('result') or {}
            original=result.get('data',{})
            unchanged=prior.get('source_hash')==task['source_hash'] and prior['operation']['kind']=='browser_inspect' and prior.get('request_arguments',prior['operation']['args'])==operation.args
            for resource in original.get('local_resources',[]):
                try:
                    file=confined(Path(task['workspace']),resource['path'])
                    with file.open('rb') as f:raw=f.read(MAX_BODY_BYTES+1)
                    unchanged=unchanged and hashlib.sha256(raw).hexdigest()==resource['sha256']
                except (OSError,PolicyError):unchanged=False
            if unchanged and result.get('status')=='succeeded' and result.get('effect')!='unknown':
                safe=redact(ModelGateway._scrub_metadata(original,getattr(self.web,'_credential_input',lambda:())()))
                return OperationResult(operation_id=operation.id,status='succeeded',effect='none',data=dict(safe,
                    target_started=False,
                    reused_from_operation=original.get('reused_from_operation',prior['operation']['id']),
                    reuse_note='The immediately preceding successful observation already contains these results. No browser was reopened and no network request was repeated. Use this evidence to answer; call history_read for details. Supply refresh_reason only if a new observation is actually needed.'))
        stop = threading.Event()
        # Windows ASGI may use SelectorEventLoop, which cannot spawn Playwright.
        # A dedicated asyncio.run loop owns the complete browser lifetime.
        worker = asyncio.create_task(asyncio.to_thread(
            lambda: asyncio.run(self._inspect(task, operation, stop, before_request))))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop.set()
            await asyncio.shield(worker)  # Save partial result and close before returning.
            raise

    async def _inspect(self, task, operation, stop, before_request):
        wait, timeout = arguments(operation.args)
        workspace = Path(task['workspace'])
        if operation.args.get('path'):
            path = confined(workspace, operation.args['path'])
            if path.suffix.lower() not in {'.html','.htm'} or not path.is_file():
                raise PolicyError('Select an existing HTML file in this task workspace')
            url = LOCAL_ORIGIN + '/' + quote(path.relative_to(workspace).as_posix(), safe='/')
            local = True
        else:
            url, _ = _validate_url(operation.args['url']);local = False
            self.web._credential_input(url)
            if self.web.settings.get().get('web_provider','none') == 'none':
                raise PolicyError('No public Web provider is configured; local HTML inspection remains available')
        data = dict(url=url, started_at=now(), javascript_executed=False, network=[], console=[], errors=[],
                    storage=[], rendered_text='', sources=[], local_resources=[], limits=[],
                    coverage={'profile':'new anonymous non-persistent context',
                              'network':'DNS-pinned public HTTPS GET/HEAD only; other attempted methods are recorded and blocked',
                              'service_workers':'blocked', 'websockets':'blocked',
                              'interaction':'page load only; no login, form submission, existing profile or permissions',
                              'local_files':'task-relative files only' if local else 'none'})
        def persist(result=None):
            known = getattr(self.web, '_credential_input', lambda: ())()
            safe = ModelGateway._scrub_metadata(data, known)
            payload = {'task_id':task['id'], 'source_hash':task['source_hash'], 'observation':safe}
            if result is not None:payload['result']=result
            self.store.record('browser_observation', operation.id, payload)
        persist()
        browser = None
        play = None
        driver_start = None
        driver_manager = None
        status, error = 'failed', ''
        total_bytes = 0
        acquisitions=[]
        broker_lock=asyncio.Lock()
        active_routes=set()
        closing=False
        preflight=None
        deadline = time.monotonic() + timeout
        context = None
        # Reserve a non-listening port: any browser connection outside the
        # intercepted broker fails closed, even if the host has a port-9 service.
        blackhole=socket.socket()
        if os.name=='nt':blackhole.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        blackhole.bind(('127.0.0.1',0))
        async def acquire(method, target, phase, headers=None):
            nonlocal total_bytes
            # Reserve the aggregate budget at the actual acquisition boundary.
            # A serialized broker also bounds live response buffers; blocked
            # attempts remain cheap and do not wait for a network operation.
            async with broker_lock:
                before_request()
                if closing or stop.is_set() or time.monotonic() >= deadline:
                    raise PolicyError('observation stopped')
                remaining=MAX_TOTAL_BYTES-total_bytes
                if remaining<=0:
                    data['limits'].append('total_response_bytes')
                    raise PolicyError('Total browser response limit reached')
                records=[]
                values=dict(self.web.settings.get(),web_max_response_bytes=min(MAX_BODY_BYTES,remaining),
                            request_timeout_seconds=max(.1,min(12,deadline-time.monotonic())))
                try:
                    return await self.web._acquire(method,target,values=values,records=records,evaluate=None,
                        task_id=task['id'],operation_id=operation.id,phase=phase,headers=headers)
                finally:
                    # Partial/failed/cancelled reads consume the same budget.
                    # Preserve unknown HTTP independently of browser cleanup.
                    total_bytes+=sum(x.get('bytes',0) for x in records)
                    acquisitions.extend({k:x[k] for k in ('id','exchange_id','url','method','phase','status',
                        'http_status','http_dispatched','bytes','partial','error_family','error_reason') if k in x} for x in records)
                    data['acquisitions']=acquisitions
        async def route_request(route, request):
            nonlocal total_bytes,preflight
            if closing:
                # Reject arrivals before any state write or broker admission.
                # Context close can deliver queued callbacks while draining.
                try:await route.abort()
                except Exception:pass
                return
            route_task=asyncio.current_task()
            active_routes.add(route_task)
            entry = dict(url=request.url, method=request.method, resource_type=request.resource_type,
                         disposition='blocked', reason='')
            if len(data['network']) >= MAX_REQUESTS:
                if 'request_limit' not in data['limits']:data['limits'].append('request_limit')
                active_routes.discard(route_task)
                await route.abort();return
            data['network'].append(entry)
            ordinal=len(data['network'])
            try:
                if stop.is_set() or time.monotonic() >= deadline:
                    raise PolicyError('observation stopped')
                before_request()
                if request.method not in {'GET','HEAD'}:
                    entry['request_body_bytes']=len(request.post_data_buffer or b'')
                    raise PolicyError('Only passive GET/HEAD requests are sent; this method was observed but blocked')
                if total_bytes >= MAX_TOTAL_BYTES:
                    data['limits'].append('total_response_bytes');raise PolicyError('Total browser response limit reached')
                parsed = urlsplit(request.url)
                if local and f'{parsed.scheme}://{parsed.netloc}' == LOCAL_ORIGIN:
                    file = confined(workspace, unquote(parsed.path).lstrip('/'))
                    limit=min(MAX_BODY_BYTES,MAX_TOTAL_BYTES-total_bytes)
                    with file.open('rb') as handle:raw=handle.read(limit+1)
                    total_bytes+=min(len(raw),limit)
                    if len(raw)>limit:raise PolicyError('Local resource exceeds browser byte limit')
                    headers={'content-type':mimetypes.guess_type(file.name)[0] or 'application/octet-stream'}
                    if headers['content-type'].startswith('text/') or file.suffix.lower() in {'.js','.json'}:
                        try:raw.decode('utf-8');headers['content-type']+='; charset=utf-8'
                        except UnicodeError:pass
                    data['local_resources'].append(dict(path=file.relative_to(workspace).as_posix(),sha256=hashlib.sha256(raw).hexdigest()))
                    code=200
                else:
                    if local:raise PolicyError('External requests from task HTML are recorded but blocked')
                    _validate_url(request.url)
                    if request.resource_type in {'media','font','image'}:
                        raise PolicyError('Media/font/image body skipped in this text and script observation')
                    outgoing = {k:v for k,v in (await request.all_headers()).items()
                                if k in {'accept','accept-language','origin','referer','cookie'}}
                    self.web._credential_input(outgoing)
                    if preflight and request.method=='GET' and request.url==preflight[0] and request.is_navigation_request():
                        _,raw,headers,observed=preflight;preflight=None
                        entry['response_reused_from_preflight']=True
                    else:
                        raw,headers,observed=await acquire(request.method,request.url,
                            'browser_observation:request:'+str(ordinal),outgoing)
                    code=observed['http_status']
                    entry['exchange_id']=observed.get('exchange_id')
                    source=observed.get('response_analysis',{}).get('source')
                    if source and not any(s.get('id')==source.get('id') for s in data['sources']):
                        # Original bytes stay in the existing protected exchange.
                        data['sources'].append({k:source[k] for k in ('id','url','sha256','response_sha256') if k in source})
                        entry['source_index']=len(data['sources'])-1
                    if 300 <= code < 400:
                        # Playwright does not re-intercept a redirect chain.
                        # Never let it escape the pinned broker via Chromium.
                        entry.update(status=code,redirect=headers.get('location'))
                        raise PolicyError('Subresource/navigation redirect observed; this redirect was not followed by the browser. Top-level HTTP redirects are resolved before navigation.')
                # httpx decoded content; remove transfer/compression lengths.
                headers={k:v for k,v in headers.items() if k.lower() not in {'content-encoding','content-length','transfer-encoding','connection'}}
                if headers.get('content-disposition','').lower().startswith('attachment'):
                    raise PolicyError('Downloads are blocked in browser observation')
                await route.fulfill(status=code, headers=headers, body=raw if request.method=='GET' else b'')
                entry.update(disposition='received', status=code, bytes=len(raw))
            except asyncio.CancelledError:
                entry['reason']='interrupted request; any HTTP exchange remains in the original record'
                raise
            except Exception as exc:
                entry['reason']=str(exc)[:1000]
                try:await route.abort()
                except Exception:pass
            finally:
                persist()
                active_routes.discard(route_task)
        async def observe():
            nonlocal browser,play,context,status,preflight,driver_start,driver_manager
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise PolicyError('Browser driver is not installed. Install the pinned OIF dependencies; no network or code action was attempted.') from None
            target=url
            if not local:
                # Resolve only the top-level HTTP chain with the existing safe
                # reader. Navigate to the observed final URL, preserving its
                # true origin instead of serving it under the old location.
                visited=set();redirects=[]
                while True:
                    if target in visited or len(visited)>=10:raise PolicyError('Browser navigation redirect cycle/limit')
                    visited.add(target);before_request()
                    if total_bytes>=MAX_TOTAL_BYTES:raise PolicyError('Total browser response limit reached')
                    raw,headers,observed=await acquire('GET',target,'browser_observation:preflight:'+str(len(visited)))
                    if not 300<=observed['http_status']<400:
                        preflight=(target,raw,headers,observed);break
                    destination=urljoin(target,headers.get('location',''))
                    redirects.append(dict(url=target,status=observed['http_status'],destination=destination,
                                          exchange_id=observed.get('exchange_id')))
                    if 'set-cookie' in headers:
                        data['limits'].append('Redirect response sets cookies; this fresh final-page context does not import redirect cookies. Session-dependent behavior is unobserved.')
                    target,_=_validate_url(destination)
                data['navigation_redirects']=redirects
                if redirects:data['coverage']['redirects']='Top-level redirects resolved by the public broker; fresh browser navigates directly to final URL. Subresource redirects are recorded but blocked.'
            driver_manager=async_playwright()
            # Keep ownership while the driver creates its pipe/process. A stop
            # during start must still acquire and close that same driver.
            driver_start=asyncio.create_task(driver_manager.start())
            play=await asyncio.shield(driver_start)
            errors=[]
            for channel in browser_channels():
                try:
                    browser=await play.chromium.launch(channel=channel, headless=True, chromium_sandbox=True,
                        timeout=min(10000,max(1,(deadline-time.monotonic())*1000)),
                        proxy={'server':'http://127.0.0.1:'+str(blackhole.getsockname()[1]),'bypass':'<-loopback>'},
                        env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','SYSTEMDRIVE','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','HOME','DISPLAY','XDG_RUNTIME_DIR'}},
                        args=['--disable-quic','--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                              '--host-resolver-rules=MAP * ~NOTFOUND','--disable-background-networking'])
                    data['browser']=dict(channel=channel or 'chromium',version=browser.version);break
                except Exception as exc:errors.append(str(exc)[:800])
            if browser is None:
                raise PolicyError('No supported browser could start. Windows: install Edge or Chrome. Other platforms: run python -m playwright install chromium. '+ '; '.join(errors))
            context=await browser.new_context(service_workers='block',accept_downloads=False,
                permissions=[], viewport={'width':1280,'height':800})
            await context.route('**/*',route_request)
            async def socket(ws):
                if not closing and len(data['network'])<MAX_REQUESTS:
                    data['network'].append(dict(url=ws.url,method='WEBSOCKET',disposition='blocked',reason='Sockets are outside passive observation'))
                await ws.close()
            await context.route_web_socket('**/*',socket)
            page=await context.new_page()
            page.set_default_timeout(3000)
            page.on('console',lambda msg: data['console'].append({'type':msg.type,'text':msg.text[:2000]}) if not closing and len(data['console'])<60 else None)
            page.on('pageerror',lambda exc: data['errors'].append(str(exc)[:2000]) if not closing and len(data['errors'])<30 else None)
            async def close_popup(popup):
                if popup!=page:await popup.close()
            context.on('page',close_popup)
            response=await page.goto(target,wait_until='domcontentloaded',timeout=max(1,(deadline-time.monotonic())*1000))
            if response is None:raise PolicyError('No main document response observed')
            await page.wait_for_timeout(wait)
            view=await page.evaluate('''() => ({title:document.title,url:location.href,
              text:(document.body?.innerText || '').slice(0,40000),
              text_truncated:(document.body?.innerText || '').length>40000,
              scripts:Array.from(document.scripts).slice(0,100).map(s=>({src:s.src,inline:!s.src,characters:s.textContent.length})),
              links:Array.from(document.links).slice(0,100).map(a=>({text:a.textContent.slice(0,150),url:a.href}))})''')
            data.update(javascript_executed=True,title=view['title'],final_url=view['url'],
                        rendered_text=view['text'],text_truncated=view['text_truncated'],scripts=view['scripts'],links=view['links'])
            for frame in page.frames[:12]:
                try:
                    item=await frame.evaluate('''() => ({origin:location.origin,
                      localStorage:Object.keys(localStorage).slice(0,64).map(k=>[k,localStorage.getItem(k)]),
                      sessionStorage:Object.keys(sessionStorage).slice(0,64).map(k=>[k,sessionStorage.getItem(k)]),
                      local_count:localStorage.length,session_count:sessionStorage.length})''')
                    for kind in ('localStorage','sessionStorage'):
                        item[kind]=[{'name':k,'value':storage_value(k,v)} for k,v in item[kind]]
                    data['storage'].append(item)
                except Exception as exc:data['storage'].append({'url':frame.url,'error':str(exc)[:500]})
            cookies=await context.cookies()
            data['cookies']=[dict(c,value=storage_value(c['name'],c['value'])) for c in cookies[:STORAGE_LIMIT]]
            data['cookie_count']=len(cookies)
            if len(cookies)>STORAGE_LIMIT:data['limits'].append('cookie_count')
            status='succeeded'
        job=asyncio.create_task(observe())
        try:
            while not job.done():
                if stop.is_set() or time.monotonic()>=deadline:
                    job.cancel();break
                await asyncio.wait({job},timeout=.05)
            await job
        except asyncio.CancelledError:
            error='Browser observation stopped; partial observations preserved.'
        except Exception as exc:
            error=str(exc)[:3000]
        finally:
            closing=True
            # Route callbacks are separate Playwright tasks. Reconcile their
            # transport outcomes before closing the driver and saving the final
            # operation, including a request still open at the observation limit.
            pending=list(active_routes)
            for pending_route in pending:pending_route.cancel()
            if pending:await asyncio.gather(*pending,return_exceptions=True)
            data['cleanup']={}
            if driver_start is not None and play is None:
                try:
                    play=await asyncio.wait_for(asyncio.shield(driver_start),5)
                except Exception as exc:
                    data['limits'].append('driver_start: '+type(exc).__name__+': '+str(exc)[:300])
            for name,obj in (('context',context),('browser',browser)):
                if obj:
                    try:
                        await asyncio.wait_for(obj.close(),5)
                        data['cleanup'][name]='closed'
                    except Exception as exc:
                        data['cleanup'][name]='unconfirmed'
                        data['limits'].append('cleanup: '+name+': '+type(exc).__name__+': '+str(exc)[:300])
            if driver_manager is not None:
                try:
                    await asyncio.wait_for(driver_manager.__aexit__(),5)
                    data['cleanup']['driver']='closed'
                except Exception as exc:
                    data['cleanup']['driver']='unconfirmed'
                    data['limits'].append('driver_cleanup: '+type(exc).__name__+': '+str(exc)[:300])
            blackhole.close()
        data.update(finished_at=now(),received_bytes=total_bytes,interrupted=stop.is_set())
        safe = redact(ModelGateway._scrub_metadata(data, getattr(self.web,'_credential_input',lambda:())()))
        # Closing the parent browser also closes a context whose own close
        # acknowledgement was lost. Retain that first fault, without pretending
        # it means the browser process is still unobserved after a successful close.
        cleanup_unknown=any(data['cleanup'].get(k)=='unconfirmed' for k in ('browser','driver'))
        transport_unknown=any(x.get('http_dispatched') and x.get('status') in {'started','unknown'} for x in acquisitions)
        uncertain=cleanup_unknown or transport_unknown
        result=OperationResult(operation_id=operation.id,status='unknown' if uncertain else status,
            effect='unknown' if uncertain else 'confirmed',data=safe,stderr=error)
        persist(result.model_dump())
        return result
