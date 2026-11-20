"""Synthetic-audio ASR benchmark. Key via DASHSCOPE_API_KEY or stdin; never saved.
Requires websocket-client for realtime models. Usage:
  DASHSCOPE_API_KEY=... python3 benchmark_asr.py /path/to/16k-mono.wav
"""
import base64
import json
import os
import pathlib
import statistics
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid
import wave
import websocket

KEY = os.environ.get('DASHSCOPE_API_KEY') or sys.stdin.readline().strip()
AUDIO = pathlib.Path(sys.argv[1]).read_bytes()
with wave.open(sys.argv[1]) as w:
    assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
    PCM = w.readframes(w.getnframes())
DURATION = len(PCM) / 32000
URI = 'data:audio/wav;base64,' + base64.b64encode(AUDIO).decode()
MODELS = ['qwen3-asr-flash', 'qwen3-asr-flash-realtime',
          'qwen-audio-3.1-asr-flash', 'qwen-audio-3.1-asr-flash-streaming']

def http_test(model):
    content = [{'type': 'input_audio', 'input_audio': {'data': URI}}]
    if model == MODELS[0]:
        url = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
        body = {'model': model, 'messages': [{'role': 'user', 'content': content}],
                'stream': False, 'asr_options': {'language': 'zh', 'enable_itn': False}}
    else:
        url = 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'
        body = {'model': model, 'input': {'messages': [{'role': 'user', 'content': content}]},
                'parameters': {'format': 'wav', 'sample_rate': 16000, 'language_hints': ['zh']}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                headers={'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'HTTP {e.code}: {e.read().decode()}') from None
    return {'total_s': time.perf_counter()-t, 'response': result}

def ws_test(model, paced=False):
    old = model == MODELS[1]
    url = ('wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model='+model if old
           else 'wss://dashscope.aliyuncs.com/api-ws/v1/inference')
    t = time.perf_counter()
    ws = websocket.create_connection(url, header={'Authorization': 'Bearer '+KEY}, timeout=45,
                                     suppress_origin=True)
    def send(x):
        ws.send(json.dumps(x))
    def recv():
        x = json.loads(ws.recv())
        if x.get('type') == 'error' or x.get('header', {}).get('event') == 'task-failed':
            raise RuntimeError(json.dumps(x, ensure_ascii=False))
        return x
    tid = uuid.uuid4().hex
    try:
        if old:
            recv()
            send({'event_id': uuid.uuid4().hex, 'type': 'session.update', 'session': {
                'input_audio_format': 'pcm', 'sample_rate': 16000,
                'input_audio_transcription': {'language': 'zh'}, 'turn_detection': None}})
            while recv().get('type') != 'session.updated':
                pass
        else:
            send({'header': {'action': 'run-task', 'task_id': tid, 'streaming': 'duplex'},
                  'payload': {'task_group': 'audio', 'task': 'asr', 'function': 'recognition',
                              'model': model, 'parameters': {'sample_rate': 16000, 'format': 'pcm'}, 'input': {}}})
            while recv().get('header', {}).get('event') != 'task-started':
                pass
        ready = time.perf_counter()
        sent = []; errors = []
        def transmit():
            try:
                for off in range(0, len(PCM), 3200):
                    chunk = PCM[off:off+3200]
                    if old:
                        send({'event_id': uuid.uuid4().hex, 'type': 'input_audio_buffer.append',
                              'audio': base64.b64encode(chunk).decode()})
                    else:
                        ws.send_binary(chunk)
                    if paced:
                        time.sleep(len(chunk)/32000)
                sent.append(time.perf_counter())
                if old:
                    send({'event_id': uuid.uuid4().hex, 'type': 'input_audio_buffer.commit'})
                    send({'event_id': uuid.uuid4().hex, 'type': 'session.finish'})
                else:
                    send({'header': {'action': 'finish-task', 'task_id': tid, 'streaming': 'duplex'},
                          'payload': {'input': {}}})
            except Exception as e:
                errors.append(str(e))
        thread = threading.Thread(target=transmit, daemon=True); thread.start()
        events = []; first = None
        while True:
            x = recv(); kind = x.get('type') if old else x.get('header', {}).get('event')
            text = x.get('transcript') or x.get('text') if old else x.get('payload', {}).get('output', {}).get('sentence', {}).get('text')
            if text:
                first = first or time.perf_counter()
                events.append(x)
            if kind in ('session.finished', 'task-finished'):
                break
        end = time.perf_counter(); thread.join(timeout=2)
        if errors: raise RuntimeError(str(errors))
        return {'total_s': end-t, 'setup_s': ready-t, 'first_text_s': None if first is None else first-ready,
                'after_audio_s': end-sent[0] if sent else None, 'events': events}
    finally:
        ws.close()

if __name__ == '__main__':
    rows = []
    print('Audio duration:', DURATION, flush=True)
    for rep in range(3):
        for model in MODELS:
            row = {'model': model, 'repeat': rep+1, 'mode': 'buffered'}
            try:
                row.update(ws_test(model) if model in (MODELS[1], MODELS[3]) else http_test(model))
            except Exception as e:
                row['error'] = str(e)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    for model in (MODELS[1], MODELS[3]):
        row = {'model': model, 'mode': 'paced'}
        try: row.update(ws_test(model, paced=True))
        except Exception as e: row['error'] = str(e)
        rows.append(row); print(json.dumps(row, ensure_ascii=False), flush=True)
    pathlib.Path('/tmp/bailian-benchmark-results.json').write_text(json.dumps(
        {'audio_duration_s': DURATION, 'results': rows}, ensure_ascii=False, indent=2))
    for model in MODELS:
        values = [r['total_s'] for r in rows if r['model']==model and r['mode']=='buffered' and 'total_s' in r]
        print(model, 'median', statistics.median(values) if values else 'FAILED', flush=True)
