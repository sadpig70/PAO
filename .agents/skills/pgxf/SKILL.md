---
name: pgxf
description: "PGXF (PPR/Gantree IndeX Framework) — file-based index system for large PG structures. Enables lazy-load subtree access, node lookup, cross-file status aggregation, and synchronization of decomposed trees. Triggers: 인덱스, 대규모 설계, 노드 찾기, 구조 조감, 상태 집계, pgxf, index, node lookup, status aggregate."
user-invocable: true
argument-hint: "build|lookup|sync|status|prune [project-name|node-name]"
---

# PGXF — PPR/Gantree IndeX Framework v1.1

> PG가 언어, PGF가 라이브러리라면, PGXF는 **파일 시스템 인덱스**다. 전체 트리를 컨텍스트에 로드하지 않고
> 필요한 노드만 정밀 접근한다.

---

## ⚠ 언제 쓰고, 언제 쓰지 말 것 (먼저 읽을 것)

PGXF는 **규모가 받쳐줄 때만** 값을 한다. 작은 프로젝트엔 순수 오버헤드다.

**쓸 것 (아래 중 하나라도)**:
- 노드 **30+** (전체 트리가 컨텍스트에 안 들어옴)
- **다중 파일** `.pgf/` (DESIGN/WORKPLAN 여러 개) 또는 `(decomposed)` 분리 존재
- 분산된 status를 **집계**하거나, "이 노드 어디 있지?"를 **반복 조회**해야 함

**쓰지 말 것 (오버헤드)**:
- 노드 **<30 단일 파일** → 전체가 컨텍스트에 들어오므로 PGF만으로 충분. 인덱스는 "있으면 깔끔한 대시보드"일 뿐 load-bearing이 아니다.
- 한 번 보고 끝낼 작업 → 인덱스 유지 비용 > 이득.

> 정직성: 23노드급에선 PGXF의 핵심 가치인 *lazy-load*가 발동하지 않는다. 임계 미만이면 `pgf status`로 충분하다.

---

## 핵심 개념

### IndexEntry (노드 1개 = 엔트리 1개)

```python
IndexEntry = {
    "node": str, "status": str, "file": str, "line": int, "depth": int,
    "parent": Optional[str], "children": list[str], "deps": list[str],
    "has_ppr": bool, "ppr_file": Optional[str], "ppr_line": Optional[int],
    "decomposed_to": Optional[str], "tags": list[str],
}
```

### 파일 경로

```text
<root>/.pgf/      # PGF 소스 (DESIGN/WORKPLAN/status)
<root>/.pgxf/
    INDEX-{Name}.json   # 프로젝트 인덱스 (파생물 — 언제든 rebuild)
    MANIFEST.json       # 멀티 프로젝트 집계 (선택)
```

`.pgxf/`는 `.pgf/`와 동일 레벨. INDEX는 **파생 산출물**(git 포함은 정책 선택 — 권장 .gitignore).

---

## 모드

| Mode | Trigger | Action |
|------|---------|--------|
| `build` | "인덱스 빌드" | `.pgf/` DESIGN/WORKPLAN 스캔 → `INDEX-{Name}.json` 생성 |
| `lookup` | "노드 찾기", "어디 있는지" | 노드명 → 파일:줄·PPR위치·상태·부모/자식/deps |
| `sync` | "동기화", "인덱스 갱신" | 전체 rebuild → diff(added/removed/modified) 리포트 |
| `status` | "상태 조감", "전체 현황" | 인덱스 기반 집계 + 트리 요약 |
| `prune` | "삭제 노드 제거" | 소스에서 사라진 고아 노드 제거 |

---

## ★ build/sync 절차 — AI 런타임이 결정론적으로 수행

**별도 스크립트가 없어도** AI 런타임이 아래 결정론 절차를 그대로 실행하면 인덱스가 만들어진다.
(같은 입력 → 같은 인덱스. 비결정론 요소 없음.)

```python
def pgxf_build(project: str) -> Index:
    # 1) 소스 수집: .pgf/ 에서 DESIGN-{project}.md, WORKPLAN-{project}.md (+ decomposed 분리 파일)
    sources = list_pgf_files(project)

    nodes = {}
    for src in sources:
        for ln, raw in enumerate(read_lines(src), start=1):
            if not is_gantree_node(raw):          # 'Name // desc (status)' 패턴만
                continue
            depth = leading_spaces(raw) // 4        # 4-space = 1 level (PG 규칙)
            name  = parse_node_name(raw)            # CamelCase 식별자
            nodes[name] = {
                "node": name, "status": parse_status(raw),  # (done|in-progress|designing|...)
                "file": src, "line": ln, "depth": depth,
                "parent": stack_parent(depth),       # 들여쓰기 스택의 직전 상위
                "children": [], "deps": parse_deps(raw),     # @dep:A,B
                "has_ppr": False, "ppr_file": None, "ppr_line": None,
                "decomposed_to": resolve_decomposed(raw) if status=="decomposed" else None,
                "tags": parse_tags(raw),             # #tag
            }
    link_parent_children(nodes)                      # 양방향 일관성
    map_ppr_defs(nodes, sources)                     # CamelCase→snake_case로 def 매칭 → has_ppr/ppr_line
    save_json(f".pgxf/INDEX-{project}.json", index(nodes))
    return index
    # acceptance_criteria:
    #   - 노드명 유일 (중복 시 경고 + 파일:줄)
    #   - parent-children 양방향 일관성
    #   - decomposed 노드의 분리 파일이 실재
    #   - 같은 소스 → byte-stable 인덱스(결정론)
```

```python
def pgxf_sync(project: str) -> Diff:
    old = load_index(project); new = pgxf_build(project)   # 전체 rebuild
    return diff(old, new)   # added / removed / modified(status·위치·PPR) → 리포트 후 저장
```

> 노드 상태가 바뀔 때마다 `sync`를 권장(수동). 노드 50+에선 `lookup` 전 `sync` 확인 필수.

---

## lookup / status 출력 예

```
[PGXF] PaymentProcessor
  📍 .pgf/DESIGN-OrderSystem.md:45   📊 in-progress
  🔗 PPR: :112 (def payment_processor)   ⬆ OrderSystem   ⬇ ValidateCard, ChargeCard   ➡ deps: UserAuth
```
```
[PGXF] OrderSystem status   done 9/15 (60%) | in-progress 3 | designing 2 | blocked 1
  🔴 blocked: PaymentGateway (external API)   🟡 decomposed: ShippingFlow → DESIGN-ShippingFlow.md
```

---

## (decomposed) 자동 추적 — PGXF의 핵심 가치

`(decomposed)` 노드의 `children`에 **분리된 파일의 자식들**을 포함시켜, 인덱스가 **파일 경계를 넘어 트리를 재구성**한다.

```
# DESIGN-OrderSystem.md
PaymentFlow // 결제 — see DESIGN-PaymentFlow.md (decomposed)
# DESIGN-PaymentFlow.md
PaymentFlow // 결제 상세 (in-progress)
    ValidateCard // (done)
```
→ 인덱스의 `PaymentFlow.children = [ValidateCard, ...]`, `decomposed_to = ".pgf/DESIGN-PaymentFlow.md"`.

---

## Lazy Load 패턴 (대규모 작업 표준 — 임계 이상에서만 발동)

```python
index = pgxf_load_index(project)                 # 1) 경량 인덱스만 로드
targets = AI_identify_relevant_nodes(task, index.summary)   # 2) 대상 노드 식별
for n in targets:                                # 3) 해당 노드의 소스 범위만 로드
    load_file_range(index.nodes[n].file, index.nodes[n].line, estimate_end(...))
execute_task(task); pgxf_sync(project)           # 4) 작업 후 동기화
```

---

## PGF 연동

| PGF 이벤트 | PGXF |
|------------|------|
| `design` 완료 | `build` (임계 이상일 때) |
| `execute` 상태 변경 | `sync` 권장 |
| `loop` 시작 | 인덱스 로드 후 실행 |
| `full-cycle` 완료 | `sync` + `status` |
| `(decomposed)` 발생 | 다음 `sync`에서 자동 추적 |

---

## 체크리스트

**Build**: 모든 DESIGN/WORKPLAN 스캔? · 노드명 중복 없음? · decomposed 분리 파일 실재? · PPR def 매핑 정확? · parent-children 양방향?
**Sync**: added/removed/modified 카운트 정확? · summary 집계가 nodes 상태와 일치? · `decomposed_to` 유효 파일?
**운영**: INDEX는 파생물(권장 .gitignore) · MANIFEST는 멀티 프로젝트에서만 · 50+에선 lookup 전 sync · **<30 단일파일이면 PGXF 자체를 쓰지 말 것**.
