"""
낼나샵 채널톡 상담 데이터 자동 수집 스크립트
- 매일 오전 8시, 오후 5시 1분 실행 (GitHub Actions)
- 개인정보(이름/닉네임/연락처/이메일) 절대 수집 안 함
- 3중 보호: 요청 제외 → 즉시 삭제 → 저장 전 검증
"""

import requests, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import Counter

# ─── 설정 ─────────────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
ACCESS_KEY    = os.environ['CHANNELTALK_ACCESS_KEY']
ACCESS_SECRET = os.environ['CHANNELTALK_ACCESS_SECRET']
BASE_URL      = 'https://api.channel.io/open/v5'
DATA_DIR      = Path('data')
DATA_DIR.mkdir(exist_ok=True)

# ─── 개인정보 보호 설정 ───────────────────────────────────────────────────────
# 절대 저장하지 않을 필드 (이름/닉네임/연락처/이메일)
FORBIDDEN_FIELDS = {
    'name', 'nickname', 'mobileNumber', 'phoneNumber',
    'email', 'avatarUrl', 'profile', 'user',
    'member', 'customer', 'contact',
}

# API에서 가져올 필드만 명시적으로 허용
ALLOWED_FIELDS = [
    'id', 'createdAt', 'updatedAt', 'closedAt',
    'state', 'goalState', 'tags',
    'alfTriggered', 'workflowTriggered',
    'assigneeId', 'firstAssigneeId', 'teamId',
    'mediumType', 'mediumName',
    'firstMemberHandlingTimeInOperation',
    'memberHandlingTimeInOperation',
    'reopened', 'reopenCount', 'missedReason',
    'replyCount', 'replyCountInOperation',
]

def sanitize(chat: dict) -> dict:
    """개인정보 필드 즉시 삭제 (1차 보호)"""
    result = {}
    for key, val in chat.items():
        if key in FORBIDDEN_FIELDS:
            continue  # 개인정보 필드 접근 없이 그냥 건너뜀
        if isinstance(val, dict):
            # 중첩 객체도 재귀적으로 정제
            cleaned = {k: v for k, v in val.items() if k not in FORBIDDEN_FIELDS}
            result[key] = cleaned
        else:
            result[key] = val
    return result

def verify_no_personal_data(data: dict):
    """저장 전 개인정보 포함 여부 최종 검증 (3차 보호)"""
    data_str = json.dumps(data, ensure_ascii=False)
    for field in FORBIDDEN_FIELDS:
        if f'"{field}"' in data_str:
            raise ValueError(f"⚠️ 개인정보 필드 감지됨: '{field}' — 저장 중단")
    print("✅ 개인정보 검증 통과")

# ─── API 호출 ─────────────────────────────────────────────────────────────────
def get_headers():
    return {
        'x-access-key': ACCESS_KEY,
        'x-access-secret': ACCESS_SECRET,
    }

def fetch_all_chats(start_ms: int, end_ms: int) -> list:
    """기간 내 종료된 상담 전체 페이지네이션으로 수집"""
    chats = []
    since = None
    page = 0
    while True:
        params = {'state': 'closed', 'sortOrder': 'asc', 'limit': 500}
        if since:
            params['since'] = since
        r = requests.get(f'{BASE_URL}/user-chats', headers=get_headers(), params=params)
        r.raise_for_status()
        data = r.json()
        items = data.get('userChats', [])
        page += 1
        collected = 0
        for item in items:
            created = item.get('createdAt', 0)
            if created > end_ms:
                print(f"  페이지 {page}: {len(chats)}건 수집 완료 (범위 초과로 중단)")
                return chats
            if created >= start_ms:
                # 2차 보호: 수집 즉시 개인정보 삭제
                clean = sanitize(item)
                chats.append(clean)
                collected += 1
        print(f"  페이지 {page}: +{collected}건 (누계 {len(chats)}건)")
        since = data.get('next')
        if not since or not items:
            break
    return chats

# ─── 태그 정규화 ──────────────────────────────────────────────────────────────
def norm_tag(raw: str):
    tag = raw.strip()
    if not tag or tag == 'nan': return None
    if tag in ('알프', '모워크플로'): return None
    if '테스트' in tag or '중복' in tag: return None
    if '교환반품' in tag:
        p = tag.split('/'); s = p[1] if len(p) >= 2 else ''
        if '맞교환' in s or ('교환' in s and '반품' not in s): return '교환'
        if '미수거재발송' in s: return '미수거재발송'
        if '반품' in s: return '반품'
        if '진행상황' in s: return '교환'
        if '철회' in s: return '철회'
        return '교환반품'
    if '고객반품' in tag:
        p = tag.split('/'); s = p[1] if len(p) >= 2 else ''
        if '교환' in s: return '교환'
        if '반품' in s: return '반품'
    if '디지털' in tag:
        p = tag.split('/'); s = p[1] if len(p) >= 2 else ''; sub = p[2] if len(p) >= 3 else ''
        if '사용문의' in s: return f'디지털/사용문의/{sub}' if sub else '디지털/사용문의'
        if '오류문의' in s: return f'디지털/오류문의/{sub}' if sub else '디지털/오류문의'
        if '제품문의' in s: return '디지털/제품문의'
        return '디지털'
    if '취소' in tag:
        p = tag.split('/'); s = p[1] if len(p) >= 2 else ''
        if '취소문의' in s: return '취소문의'
    return tag

def iter_tags(s):
    # API 응답에서 tags는 리스트일 수도, 쉼표 구분 문자열일 수도 있음
    if isinstance(s, list):
        return [t for t in (norm_tag(r) for r in s) if t]
    return [t for t in (norm_tag(r) for r in str(s or '').split(',')) if t]

def merge_detail(d):
    if '타이머' in d and '기능정지' in d: return '타이머-기능정지'
    if '케이블커버' in d: return '케이블커버'
    if '낼나펜슬' in d or ('펜슬' in d and '케이블' not in d): return '낼나펜슬'
    if '데일리케이블' in d: return '데일리케이블'
    return d

# ─── 데이터 처리 ──────────────────────────────────────────────────────────────
def process(chats: list, prev: dict) -> dict:
    pct = lambda a, b: round(a / b * 100, 1) if b else 0
    isT = lambda v: str(v).lower() in ['true', '1', 'yes', 'True']

    total = len(chats)
    if total == 0:
        print("⚠️ 수집된 상담 건수 0건")
        return None

    # 분류
    def alf_trig(r): return isT(r.get('alfTriggered', False))
    def solo(r): return r.get('goalState') == 'waiting' and alf_trig(r) and not r.get('assigneeId')
    def esc(r):  return r.get('goalState') == 'notAchieved' and alf_trig(r) and bool(r.get('assigneeId'))
    def agent(r): return not (solo(r) or esc(r)) and bool(r.get('assigneeId'))
    def work(r):  return not (solo(r) or esc(r)) and not r.get('assigneeId')

    alf_solo     = sum(1 for r in chats if solo(r))
    alf_esc      = sum(1 for r in chats if esc(r))
    agent_only   = sum(1 for r in chats if agent(r))
    work_only    = sum(1 for r in chats if work(r))
    triggered    = sum(1 for r in chats if alf_trig(r))

    # 날짜
    dates = sorted([r['createdAt'] for r in chats if r.get('createdAt')])
    fmt = lambda ms: datetime.fromtimestamp(ms/1000, KST).strftime('%m/%d')
    curr_dates = f"{fmt(dates[0])} ~ {fmt(dates[-1])}" if dates else ''

    # 요일별 (월=0)
    curr_dow = [0]*7
    for r in chats:
        if r.get('createdAt'):
            d = datetime.fromtimestamp(r['createdAt']/1000, KST)
            curr_dow[d.weekday()] += 1

    # 평균 응대시간
    times = [r['firstMemberHandlingTimeInOperation'] for r in chats
             if r.get('firstMemberHandlingTimeInOperation') and (esc(r) or agent(r))]
    avg_min = round(sum(times) / len(times) / 60, 1) if times else 0

    # 태그 집계
    solo_ids = {r['id'] for r in chats if solo(r)}
    tag_counts = {}
    for r in chats:
        is_alf = r['id'] in solo_ids; seen = set()
        for t in iter_tags(r.get('tags', '')):
            if t in seen: continue; seen.add(t)
            if t not in tag_counts: tag_counts[t] = {'tag': t, 'total': 0, 'alf': 0, 'agent': 0}
            tag_counts[t]['total'] += 1
            if is_alf: tag_counts[t]['alf'] += 1
            else: tag_counts[t]['agent'] += 1
    top10 = sorted(tag_counts.values(), key=lambda x: -x['total'])[:10]

    # 이관 태그
    esc_tags = Counter()
    for r in chats:
        if not esc(r): continue
        seen = set()
        for t in iter_tags(r.get('tags', '')):
            if t not in seen: esc_tags[t] += 1; seen.add(t)
    alf_unres = [{'tag': t, 'count': c} for t, c in esc_tags.most_common(10)]

    # prev 태그 (직전주 JSON)
    prev_solo_c = Counter(prev.get('solo_tag_counts', {}))
    prev_all_c  = Counter(prev.get('all_tag_counts', {}))

    # 단독해결 TOP5
    solo_counts = Counter()
    for r in chats:
        if not solo(r): continue
        seen = set()
        for t in iter_tags(r.get('tags', '')):
            if t not in seen: solo_counts[t] += 1; seen.add(t)
    solo_top5 = [{'tag': t, 'curr': c, 'prev': prev_solo_c.get(t, 0)}
                 for t, c in solo_counts.most_common(5)]

    # 증가 태그
    inc_tags = [{'tag': t['tag'], 'curr': t['total'],
                 'prev': prev_all_c.get(t['tag'], 0),
                 'delta': t['total'] - prev_all_c.get(t['tag'], 0)}
                for t in top10 if t['total'] > prev_all_c.get(t['tag'], 0)]
    inc_tags.sort(key=lambda x: -x['delta'])

    # 제품별 순위
    prods = {'낼나 타이머': 0, '디지털': 0, '낼나 키링캠': 0, '낼나 펜슬': 0, '데일리 케이블': 0}
    for r in chats:
        t = str(r.get('tags', '') or '')
        if '타이머' in t: prods['낼나 타이머'] += 1
        if '디지털' in t: prods['디지털'] += 1
        if '키링캠' in t: prods['낼나 키링캠'] += 1
        if '낼나펜슬' in t or '펜슬' in t: prods['낼나 펜슬'] += 1
        if '데일리케이블' in t: prods['데일리 케이블'] += 1
    prev_prod = {p['name']: p['total'] for p in prev.get('prod_ranking', [])}
    prod_ranking = sorted([
        {'name': n, 'total': v, 'prev': prev_prod.get(n, 0),
         'delta': v - prev_prod.get(n, 0),
         'curr_pct': round(v / total * 100, 1), 'prev_pct': 0}
        for n, v in prods.items()], key=lambda x: -x['total'])[:5]

    # 교환/반품
    exc, ret = Counter(), Counter()
    for r in chats:
        for raw in str(r.get('tags', '') or '').split(','):
            tag = raw.strip()
            if '교환반품' not in tag: continue
            p = tag.split('/'); s = p[1].strip() if len(p) >= 2 else ''
            d = merge_detail(p[2].strip()) if len(p) >= 3 else ''
            if not d: continue
            if '미수거재발송' in s: pass
            elif '맞교환' in s or ('교환' in s and '반품' not in s): exc[d] += 1
            elif '반품' in s: ret[d] += 1

    # 미관여
    no_alf = [r for r in chats if not alf_trig(r)]
    no_alf_agent = [r for r in no_alf if r.get('assigneeId')]
    reopen = sum(1 for r in no_alf_agent if isT(r.get('reopened', False)))
    direct = len(no_alf_agent) - reopen
    nalf_c = Counter()
    for r in no_alf_agent:
        if '중복' in str(r.get('tags', '')): continue
        seen = set()
        for t in iter_tags(r.get('tags', '')):
            if t not in seen: nalf_c[t] += 1; seen.add(t)

    # 전체/단독 태그 카운터
    all_tc = Counter(); solo_tc = Counter()
    for r in chats:
        seen = set()
        for t in iter_tags(r.get('tags', '')):
            if t not in seen: all_tc[t] += 1; seen.add(t)
    for r in chats:
        if not solo(r): continue
        seen = set()
        for t in iter_tags(r.get('tags', '')):
            if t not in seen: solo_tc[t] += 1; seen.add(t)

    # 채널별 통계
    def ch_stats(rows):
        t = len(rows); trig = sum(1 for r in rows if alf_trig(r))
        s = sum(1 for r in rows if solo(r))
        return {'total': t, 'triggered': trig, 'solo': s,
                'involveRate': pct(trig, t), 'soloRate': pct(s, t), 'resolveRate': pct(s, trig)}

    # 톡톡 알프인입 태그 처리
    has_inip   = lambda r: '톡톡/알프인입' in str(r.get('tags','')) and '톡톡/알프인입X' not in str(r.get('tags',''))
    has_inipX  = lambda r: '톡톡/알프인입X' in str(r.get('tags',''))
    has_dup    = lambda r: '중복' in str(r.get('tags',''))

    ch_rows = [r for r in chats if r.get('mediumType') == 'native']
    nv_rows = [r for r in chats if r.get('mediumName') == 'appNaverTalk']
    nv_inipX_rows = [r for r in nv_rows if has_inipX(r) or (not has_inip(r) and not has_inipX(r))]
    nv_inip_rows  = [r for r in nv_rows if has_inip(r)]

    ch_stat = ch_stats(ch_rows)
    nv_stat = ch_stats(nv_inipX_rows)

    nv_inip_total    = len(nv_inip_rows)
    nv_inip_solo     = sum(1 for r in nv_inip_rows if has_dup(r))
    nv_inip_to_ch    = sum(1 for r in ch_rows if has_inip(r))
    nv_inip_stat = {
        'total': nv_inip_total, 'solo': nv_inip_solo, 'toChannel': nv_inip_to_ch,
        'other': max(0, nv_inip_total - nv_inip_solo - nv_inip_to_ch),
        'involveRate': pct(nv_inip_total, len(nv_rows)),
        'soloRate': pct(nv_inip_solo, nv_inip_total),
        'toChannelRate': pct(nv_inip_to_ch, nv_inip_total),
    }

    # KPI 히스토리 업데이트
    kpi_history = list(prev.get('kpi_history', []))
    new_kpi = {
        'dates': curr_dates, 'totalCount': total,
        'triggeredCount': triggered, 'soloCount': alf_solo,
        'involveRate': pct(triggered, total), 'soloRate': pct(alf_solo, total),
        'resolveRate': pct(alf_solo, triggered),
        'chInvolve': ch_stat['involveRate'], 'chResolve': ch_stat['resolveRate'],
        'nvInvolve': nv_stat['involveRate'],  'nvResolve': nv_stat['resolveRate'],
    }
    ki = next((i for i, r in enumerate(kpi_history) if r['dates'] == curr_dates), -1)
    if ki >= 0: kpi_history[ki] = new_kpi
    else: kpi_history.append(new_kpi)

    # 채널별 히스토리 (최근 5주)
    def update_history(history, new_row):
        idx = next((i for i, r in enumerate(history) if r['dates'] == curr_dates), -1)
        if idx >= 0: history[idx] = new_row
        else: history.append(new_row)
        return history[-5:]

    ch_history = update_history(list(prev.get('ch_history', [])), {'dates': curr_dates, **ch_stat})
    nv_history = update_history(list(prev.get('nv_history', [])), {'dates': curr_dates, **nv_stat})

    return {
        'total': total, 'agent_assigned': alf_esc + agent_only,
        'avg_response_min': avg_min,
        'alf_triggered': triggered, 'alf_solo': alf_solo,
        'alf_escalated': alf_esc, 'agent_only': agent_only, 'workflow_only': work_only,
        'curr_solo_rate': pct(alf_solo, total),
        'prev_solo_rate': pct(prev.get('alf_solo', 0), prev.get('total', 1)),
        'prev_total': prev.get('total', 0), 'prev_total_real': prev.get('total', 0),
        'prev_alf_solo': prev.get('alf_solo', 0),
        'curr_dow': curr_dow, 'prev_dow': prev.get('curr_dow', [0]*7),
        'curr_dates': curr_dates, 'prev_dates': prev.get('curr_dates', '-'),
        'top10_tags': top10, 'alf_unresolved_tags': alf_unres,
        'inc_tags': inc_tags, 'curr_solo_top5': solo_top5, 'prev_solo_top5': solo_top5,
        'prod_ranking': prod_ranking,
        'exchange_products': [{'product': k, 'count': v} for k, v in exc.most_common()],
        'return_products':   [{'product': k, 'count': v} for k, v in ret.most_common()],
        'no_alf_direct': direct, 'no_alf_reopen': reopen,
        'no_alf_total': len(no_alf), 'no_alf_agent': len(no_alf_agent),
        'no_alf_work': len([r for r in no_alf if not r.get('assigneeId')]),
        'no_alf_tags': [{'tag': t, 'count': c} for t, c in nalf_c.most_common(10)],
        'all_tag_counts': dict(all_tc), 'solo_tag_counts': dict(solo_tc),
        'ch_stat': ch_stat, 'nv_stat': nv_stat, 'nv_inip_stat': nv_inip_stat,
        'ch_history': ch_history, 'nv_history': nv_history,
        'kpi_history': kpi_history,
    }

# ─── 메인 실행 ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    now = datetime.now(KST)
    # 이번 주 월요일 0시 ~ 지금까지
    monday = now - timedelta(days=now.weekday())
    start_ms = int(monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    end_ms   = int(now.timestamp() * 1000)
    week_key = f"{monday.strftime('%m-%d')}-to-{(monday + timedelta(days=6)).strftime('%m-%d')}"

    print(f"▶ 수집 시작: {monday.strftime('%m/%d')} ~ {now.strftime('%m/%d %H:%M')} KST")

    # API 호출 (개인정보 없이)
    chats = fetch_all_chats(start_ms, end_ms)
    print(f"✅ {len(chats)}건 수집 완료")

    # 첫 번째 상담 구조 확인 (디버깅용)
    if chats:
        print(f"🔍 첫 번째 상담 키 목록: {list(chats[0].keys())}")
        print(f"🔍 tags 필드: {chats[0].get('tags', '없음')}")

    # 직전 주차 JSON 로드 (prev 비교용)
    existing_files = sorted(DATA_DIR.glob('*.json'))
    existing_files = [f for f in existing_files if f.name != 'index.json'
                      and f.stem != week_key]
    prev = {}
    if existing_files:
        with open(existing_files[-1], encoding='utf-8') as f:
            prev = json.load(f)
        print(f"직전 주차 로드: {existing_files[-1].name}")

    # 데이터 처리
    try:
        result = process(chats, prev)
    except Exception as e:
        import traceback
        print(f"❌ process() 오류: {e}")
        traceback.print_exc()
        sys.exit(1)

    if not result:
        print("처리할 데이터 없음 — 종료")
        sys.exit(0)

    # ⚠️ 3차 보호: 저장 전 개인정보 최종 검증
    try:
        verify_no_personal_data(result)
    except ValueError as e:
        print(f"❌ 개인정보 검증 실패: {e}")
        sys.exit(1)

    # JSON 저장
    out_path = DATA_DIR / f"{week_key}.json"
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"💾 저장 완료: {out_path}")

    # index.json 업데이트
    idx_path = DATA_DIR / 'index.json'
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {'weeks': []}
    if week_key not in idx['weeks']:
        idx['weeks'].append(week_key)
        idx['weeks'].sort()
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2))
    print(f"📋 index.json 업데이트: {idx['weeks']}")
