# PAO ADP Work Plan

## POLICY

```python
POLICY = {
    "_version": "2.6",
    "max_retry": 3,
    "on_blocked": "halt",
    "design_modify_scope": ["impl", "internal_interface", "public_interface"],
    "completion": "all_done",
    "max_iterations": 30,
    "max_verify_cycles": 2,
}
```

## Execution Tree

```text
PAOADP // ADP 기반 PAO 구체화 (done) @v:1.0
    DefineProtocol // mailbox·identity·state 경계 확정 (done)
    ImplementCommon // 원자 JSON·lock·경로 유틸리티 구현 (done) @dep:DefineProtocol
    ImplementRegistry // OA 등록·lifecycle 승인 구현 (done) @dep:ImplementCommon
    ImplementOATools // Task 게시·control·result 회수·복구 구현 (done) @dep:ImplementRegistry
    ImplementWatcher // 1초 polling·90초 timeout watcher 구현 (done) @dep:ImplementCommon
    ImplementLwarTools // 등록·상태·결과 제출 구현 (done) @dep:ImplementRegistry,ImplementWatcher
    DefineSchemas // Task·Result·ADP event·control Schema 구현 (done) @dep:DefineProtocol
    UpdateLwarSkill // LWAR ADP 행동 계약 작성 (done) @dep:ImplementLwarTools,DefineSchemas
    CreateOASkill // OA 운영 행동 계약 작성 (done) @dep:ImplementOATools,DefineSchemas
    UpdateArchitecture // PAO 기본 설계를 OA·ADP 모델로 교체 (done) @dep:UpdateLwarSkill,CreateOASkill
    BuildTests // 단위·통합 테스트 구현 (done) @dep:ImplementOATools,ImplementLwarTools
    VerifySystem // acceptance·quality·architecture 검증 (done) @dep:UpdateArchitecture,BuildTests
```
