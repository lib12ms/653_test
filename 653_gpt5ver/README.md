# 653_gpt5ver — GPT-5 전환 버전 (비교용 별도 배포)

기존 `backend/` · `streamlit_app/`(gpt-4o 기반, 운영 중)는 전혀 건드리지 않고,
이 폴더 하나에 백엔드+프론트엔드를 통째로 복사해 GPT-5로 전환했습니다.
두 버전을 별도 Render 서비스로 각각 배포해 나란히 비교할 수 있도록 독립 실행 가능한
구조로 구성했습니다.

## 이전에 gpt-5 전환이 실패했던 이유

지난 시도(2026-05-28, `f953795` 커밋으로 gpt-4o 복원)에서 gpt-5는
**reasoning 토큰만으로 `max_output_tokens` 예산(200)을 전부 소진**해 가시 텍스트를
한 글자도 못 내는 문제가 있었습니다. instructions/input 통합 등 프롬프트 쪽 접근은
두 가지 다 실패했다고 기록되어 있는데, 원인은 프롬프트가 아니라 **API 호출 파라미터**
쪽이었습니다.

### 이번에 적용한 수정 (`backend/app/ai_service.py`, `_call_learned_agent_api`)

1. **`reasoning.effort` 파라미터 추가** — gpt-5류 모델에서만 `{"effort": "minimal"}`을
   전달해 reasoning 토큰 소모 자체를 최소화 (`config.py`의 `OPENAI_REASONING_EFFORT`로 조절 가능).
2. **`max_output_tokens` 예산 상향** — 200 → 1200 (`OPENAI_MAX_OUTPUT_TOKENS`). effort를
   minimal로 낮춰도 reasoning 모델은 예산을 reasoning+가시출력이 함께 나눠 쓰므로,
   기존 gpt-4o 기준값(200)은 애초에 너무 작았습니다.
3. **예산 소진 자동 재시도** — 그래도 `status="incomplete"`이고
   `incomplete_details.reason="max_output_tokens"`이면(가시 텍스트 0글자) 예산을
   2배로 늘려 한 번 더 호출. 조용히 텍스트 fallback으로 품질이 떨어지는 대신
   API 레벨에서 스스로 복구하도록 함.
4. `_is_reasoning_model()`로 모델명(`gpt-5*`/`o1*`/`o3*`/`o4*`) 기준 분기 — 이 파라미터들은
   비-reasoning 모델(gpt-4o 등)에는 전달하면 API 오류가 나므로 반드시 조건부로 넣어야 함.

실제 검증: `9788927723660`(나의 겁 없는 중국뉴스 중국어) 메타로 `/api/field653/preview`
호출 → `completion_tokens=47`, 정상 키워드(`중국어뉴스·듣기훈련·중급자·오디오교재·뉴스중국어`)
출력 확인. 예산 소진 없이 1회 호출로 성공.

### 그 외 코드는 100% 동일

`ai_service.py`의 프롬프트(`CATEGORY_PROMPTS`, `_STATIC_INSTRUCTIONS`), 필터 로직
(`finalize_653`, `_is_low_value_keyword`, `_extract_backup_candidates` 등), 그리고
`aladin_client.py`·`nlk_client.py`·`main.py`·`models.py`·`preprocess.py`·`fetcher*.py`·
`sheets_service.py`·`streamlit_app/`는 원본에서 그대로 복사했습니다(수정 없음). 즉 두
버전의 키워드 품질 차이는 순수하게 **모델 자체(gpt-4o vs gpt-5)** 에서만 나옵니다.

## 로컬 실행

```bash
cd 653_gpt5ver
pip install -r requirements.txt

# 백엔드
cd backend
uvicorn app.main:app --reload --port 8000

# 프론트(별도 터미널)
cd ../streamlit_app
streamlit run app.py
```

`.env`는 이미 채워져 있습니다(gitignore 대상이라 커밋되지 않음). 필요 시
`.env.example`을 참고하세요. `OPENAI_MODEL`을 `gpt-4o`로 바꾸면 이 폴더 안에서도
기존 방식으로 되돌아갑니다 — 두 모델을 같은 폴더에서 즉시 스위칭 비교할 때 유용합니다.

## Render 배포 시 참고

기존 `six53-test` 서비스와 완전히 분리된 **새 서비스 2개**(백엔드 web service +
Streamlit web service)로 올리는 것을 전제로 구성했습니다.

- 백엔드 Root/Start: `653_gpt5ver/backend`, `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Streamlit Root/Start: `653_gpt5ver`, `streamlit run streamlit_app/app.py --server.port $PORT --server.address 0.0.0.0`
- Streamlit 쪽 `secrets.toml`의 `BACKEND_URL`을 새 백엔드 서비스의 Render URL로 변경
- 환경변수는 `.env.example` 항목 그대로 Render 대시보드에 등록 (특히
  `OPENAI_MODEL=gpt-5`, `OPENAI_REASONING_EFFORT=minimal`, `OPENAI_MAX_OUTPUT_TOKENS=1200`)

git push·Render 업로드 자체는 별도로 진행하기로 했습니다.
