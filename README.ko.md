<h1 align="center">Shisei(拾戏): 완전 오픈소스·셀프호스팅 AI 영상 스튜디오 (dsh 플러그인)</h1>
<p align="center"><b>숏드라마</b>: 한 문장으로 세로형 숏드라마를 찍습니다 · <b>리메이크</b>: 댄스 영상 속 인물을 AI 캐릭터로 바꿉니다<br>모두 내 클라우드 GPU에서 돌아갑니다</p>
<p align="center"><a href="README.md">简体中文</a> · <a href="README.en.md">English</a> · <a href="README.ja.md">日本語</a> · <b>한국어</b></p>
<p align="center">
  <a href="https://shisei.indaloop.app/"><img src="https://img.shields.io/badge/Website-shisei.indaloop.app-E8552B?style=for-the-badge" alt="Website" height="36"></a>
  <a href="https://x.com/wquguru"><img src="https://img.shields.io/badge/Follow-@wquguru-000000?style=for-the-badge&logo=x&logoColor=white" alt="Follow @wquguru on X" height="36"></a>
</p>
<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0%20%2B%20GPL--3.0%20workflows-blue" alt="License: Apache-2.0, three workflows GPL-3.0">
  <img src="https://img.shields.io/badge/DeepSeek%20Harness-plugin-4D6BFE" alt="DeepSeek Harness plugin">
  <img src="https://img.shields.io/badge/UI-中文%20%7C%20EN%20%7C%20日本語%20%7C%20한국어-555" alt="UI: Chinese, English, Japanese, Korean">
  <img src="https://img.shields.io/badge/tested%20on-macOS%20%7C%20Windows-555" alt="Tested on macOS and Windows">
</p>
<p align="center">⭐ 쓸 만하다면 나중에 다시 찾기 쉽게 Star를 눌러 주세요</p>
<p align="center"><img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/demo.gif" width="800" alt="홈 화면에서 『晾衣绳』(빨랫줄) 프로젝트를 열고, 감독 대화창에서 구간 B의 첫 프레임을 반려해 클로즈업으로 다시 찍게 한 대화와 견적을 되짚어 본 뒤, 구간 B의 새 첫 프레임과 대사, 대사 일치 표시가 붙은 후보 테이크를 확인하고, 40초 완성본을 미리 보며 @로 캐릭터와 구간을 지정하는 모습"></p>
<p align="center"><sub>『晾衣绳』(빨랫줄) 실제 작업 화면. 구간 B를 반려해 클로즈업으로 다시 찍고, 테이크를 골라 40초 완성본을 미리 본 뒤 @로 지정해 샷을 고칩니다.</sub></p>

직접 돌려 보니 30초 숏드라마 한 편이 최저 ¥3(위안)이면 나옵니다. Shisei(`dsh-shisei`)는 제가 만든 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)(`dsh`, 웹 UI를 갖춘 DeepSeek의 오픈소스 에이전트 도구)용 플러그인입니다. 코드는 Apache-2.0으로 공개되어 있고(워크플로 세 개는 GPL-3.0), `shisei.indaloop.app`의 CC0 공개 에셋 라이브러리를 온라인으로 불러옵니다.

- **쓴 만큼 초 단위 과금, 구독료 0원**: 플러그인은 무료이고 자체 서버도 없습니다. 대본 작성, 이미지 검토, 테이크 선택, 합성은 모두 로컬에서 돌아가 비용이 들지 않습니다. 화면을 생성할 때만 내 [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc)(优云智算) GPU를 빌립니다. 실제로 29.4초짜리 『末班车』(막차)는 약 ¥3, 40초짜리 『晾衣绳』(빨랫줄)는 유휴 대기 포함 약 ¥5.9가 들었습니다.
- **틀린 샷만 골라서 수정**: `@`로 캐릭터·소품·구간을 지정하거나 화면에 동그라미를 그려 감독과 이야기하면 그 구간만 다시 찍습니다. 이전 버전으로는 언제든 되돌릴 수 있습니다.
- **GPU 가동과 과금은 전부 내가 승인**: LLM 감독은 콘티 작성과 제안만 맡습니다. 코드로 막혀 있어 GPU를 켤 수 없고 나 대신 이미지를 승인할 수도 없습니다. 프로젝트마다 기본 예산 한도는 ¥5입니다.

## 빠르게 시작하기

<details>
<summary><b>설치 전에 준비할 것</b></summary>

- **시스템**: macOS 또는 Windows 11, [`dsh`](https://github.com/deepseek-ai/deepseek-harness) 0.2.x(웹 UI). dsh 데스크톱 버전이면 따로 설치할 게 없어요. Python은 데스크톱 버전에 들어 있는 것을 쓰고, ssh와 compshare용 도구는 플러그인에 들어 있으며, ffmpeg가 없으면 Shisei를 처음 열 때 뒤에서 내려받아요. GPU 머신에 로그인할 ssh 키는 머신을 만들 때 자동으로 만들어요. 명령줄 버전 dsh에서는 [Node](https://nodejs.org/) 20+와 [Python](https://www.python.org/downloads/) 3.9+(Windows는 `py` 런처가 포함된 64비트 설치 파일)도 필요해요.
- **오디오/비디오**: `ffmpeg`(레퍼런스 영상 기능은 4.4+ 필요. 없으면 플러그인 「설정」에서 한 번에 설치). 선택 사항으로 `whisper.cpp`(대사 대조용).
- **감독 모델**: `dsh`에 LLM을 하나 설정합니다. 「시작」 페이지는 Ant MaaS의 Ling-3.2-Flash를 추천합니다(이미지를 볼 수 있고 무료 제공량이 있음).
- **GPU**: [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc)(优云智算)에 가입해 실명 인증과 충전을 마치고 콘솔에서 API 키를 발급받습니다. 그래픽카드는 4090 48G를 씁니다.
- **언어**: UI는 简体中文, English, 日本語, 한국어를 지원하고, 대본과 자막은 중국어로 작성됩니다.

</details>

npm에서 설치합니다. **버전 번호를 꼭 붙이세요**. dsh에 들어 있는 pnpm은 공개된 지 하루가 안 된 버전을 고르지 않아서, `dsh-shisei`만 쓰면 이전 버전이 설치될 수 있습니다.

**dsh 데스크톱**: dsh 플러그인 페이지에서 "Add plugin"을 누르고 `dsh-shisei@0.2.2`를 입력한 뒤 Install, dsh를 완전히 종료했다가 다시 엽니다. 데스크톱 프로필은 앱에서만 바꿀 수 있어 아래 명령은 통하지 않습니다.

**명령줄 dsh**:

```bash
# 처음 한 번만: dsh 내장 web 템플릿으로 프로필을 만듭니다. 이미 있다면 건너뛰고 아래에서 그 프로필 이름을 쓰세요
dsh --profile web --from-default-profile web --dump-config >/dev/null
dsh plugin --profile web add dsh-shisei@0.2.2
dsh --profile web
```

코드를 고치거나 최신 main을 쓰려면 git으로 클론해서 설치합니다.

```bash
git clone https://github.com/wquguru/dsh-shisei.git
dsh plugin --profile web add "$PWD/dsh-shisei"
```

<details>
<summary><b>플러그인 업그레이드</b></summary>

새 버전이 나오면 플러그인 사이드바에 "새 버전" 알림이 뜨고, "설정" 맨 위 "버전" 카드가 쓰고 있는 dsh에 맞는 단계를 보여 줍니다. 설치하고 다시 시작하면 업그레이드됐다고 알려 줍니다.

- **dsh 데스크톱**: 그 자리에서 업그레이드할 수 없습니다. 플러그인 페이지에서 dsh-shisei를 먼저 **제거**하고, "Add plugin"에 `dsh-shisei@새 버전`을 입력해 Install한 뒤 dsh를 완전히 종료했다가 다시 엽니다. 제거를 건너뛰면 `This plugin is already installed`가 뜹니다.
- **명령줄 dsh**: `dsh plugin --profile web add dsh-shisei@새 버전`을 실행하고 dsh를 다시 시작합니다.
- **새 버전을 찾을 수 없거나 이전 버전이 설치됨**: 미러가 아직 동기화되지 않았을 수 있습니다. 공식 레지스트리로 바꿔 다시 설치하세요(데스크톱은 설치 창 오른쪽 Registry에서 고릅니다).
- 프로젝트, 설정, 비용 장부는 `~/.dsh/shisei/`에 있고 제거나 업그레이드로 지워지지 않습니다. 1.0 전에는 minor 버전(예: 0.1 → 0.2)이 바뀌면 호환되지 않는 변경이 있을 수 있으니 먼저 [변경 기록](https://github.com/wquguru/dsh-shisei/blob/main/CHANGELOG.md)을 읽으세요.

</details>

`dsh` 웹 화면 왼쪽 사이드바에서 「Shisei」를 열고, 「시작」 페이지 안내대로 감독 모델, [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) 키, GPU 머신을 설정하면 바로 촬영할 수 있습니다. 연습용 대본 세 편 『打烊之后』(마감 후에), 『末班车』(막차), 『晾衣绳』(빨랫줄)를 복사해서 시작해 보세요.

GPU 머신 만들기는 약 10분, ¥1 미만이에요. 플러그인이 환경이 갖춰진 무료 Shisei 커뮤니티 이미지로 만듭니다.

## 할 수 있는 일

| 작업 | 사용자가 할 일 | 결과물 |
| :--- | :--- | :--- |
| **세로형 숏드라마 촬영** | 한 줄 이야기를 쓰고, 캐릭터·장면·소품·첫 프레임을 한 장씩 검토한 뒤 「GPU 켜고 촬영」을 누르고, 여러 테이크 중 하나를 고릅니다 | 대사, 속마음 독백, 환경음, 균일한 음량, 소프트 자막이 들어간 워터마크 없는 9:16 MP4 완성본 |
| **대화로 샷 수정** | 오른쪽 패널에서 `@`나 동그라미로 감독에게 불만을 말하고, 새 콘티와 견적을 확인한 뒤 다시 찍습니다 | 지정한 구간만 재촬영. 이전 첫 프레임과 영상은 언제든 되돌릴 수 있음 |
| **레퍼런스 영상의 연출 따라 쓰기** | 로컬 참고 영상(최대 3분)을 고르고 내 이야기 한 줄을 줍니다 | 레퍼런스의 샷 크기, 카메라 워크, 조명을 따라 쓴 새 대본(연출만 배우며 원본은 업로드하지 않음) |
| **시리즈 촬영** | 여러 편을 한 시리즈로 묶고, 검토를 마친 캐릭터를 고정 출연으로 정하고, 캐릭터 자신의 대사 한 줄로 목소리를 고정합니다 | 새 편에 검토를 마친 캐릭터와 고정한 목소리가 그대로 들어가고, 감독은 확정한 지난 줄거리를 바탕으로 다음 편을 씁니다 |
| **리메이크** | 직접 찍었거나 쓸 권리가 있는 댄스 영상(2~15초 구간, 또는 최대 5분 전체), 성인 캐릭터의 전신 이미지와 얼굴 클로즈업(AI 생성이든, 쓸 권리가 있는 실제 인물이든), 의상 설명 한 줄을 올립니다 | 원본 영상 / 3D 클레이 모델 / 리메이크 결과를 나란히 놓은 3단 비교 영상 |

## 실제 테스트 사례

아래 세 완성본은 모두 제 4090 48G(당시 시간당 ¥3.8)로 직접 돌려 만든 결과입니다.

<table>
  <tr>
    <td width="33%" valign="top">
      <img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/cases/drama-last-bus.jpg" alt="『末班车』(막차) 완성본에서 뽑은 세 프레임: 밤 버스 안의 두 캐릭터, 가운데 프레임은 여자 주인공 클로즈업, 9:16 세로형, 오른쪽 위에 AI 생성 표시"><br>
      <b>숏드라마 『末班车』(막차)</b> · 5샷 / 캐릭터 2명 / 소품 1개<br>
      샷마다 2테이크씩, 두 번 가동해 총 약 45분 만에 29.4초 완성본을 만들었습니다. 이어서 불만 세 줄만 적어 감독이 5샷을 고쳐 v2를 다시 찍게 했고, 약 40분이 더 걸렸습니다.<br>
      <b>비용</b>: 첫 버전 약 ¥3, v2 수정 약 ¥2.5
    </td>
    <td width="33%" valign="top">
      <img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/cases/drama-clothesline.jpg" alt="『晾衣绳』(빨랫줄) 완성본에서 뽑은 세 프레임: 해 질 녘 옥상에서 이불을 너는 남녀, 남자는 귤 한 봉지를 들고 있음, 9:16 세로형, 오른쪽 위에 AI 생성 표시"><br>
      <b>숏드라마 『晾衣绳』(빨랫줄)</b> · 4구간 11샷 / 캐릭터 2명<br>
      다섯 번에 나눠 찍어 40.0초 세로형 완성본을 만들었습니다.<br>
      <b>비용</b>: 약 ¥5.9(유휴 대기 포함)
    </td>
    <td width="33%" valign="top">
      <img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/cases/motion-dance.jpg" alt="댄스 리메이크 시험 촬영의 세 프레임: 왼쪽은 3D 클레이 모델, 가운데와 오른쪽은 같은 순간의 캐릭터 교체 테이크 두 개로, 빨간 땋은 머리에 세이지색 티셔츠와 반바지를 입은 AI 캐릭터, 세로형"><br>
      <b>댄스 리메이크</b> · 시험 촬영 4.5초 × 3테이크<br>
      모션 캡처와 클레이 모델을 돌린 뒤 고른 구간의 춤으로 AI 캐릭터 테이크 3개를 찍었습니다. GPU 한 번 켜서 약 19분.<br>
      <b>비용</b>: 약 ¥1.82(유휴 대기 10분 포함)
    </td>
  </tr>
</table>

<details>
<summary><b>테스트 상세 기록과 알려진 한계</b></summary>

| 날짜 | 작업 | 규모 | 소요 시간 | 비용 |
| :--- | :--- | :--- | :--- | :--- |
| 2026-11-10 | 숏드라마 『末班车』(막차) | 5샷, 캐릭터 2명, 소품 1개, 샷당 2테이크, 완성본 29.4초 | 2회 가동 총 약 45분 | 약 ¥3 |
| 2026-11-10 | 대화로 샷 수정 『末班车 v2』(막차 v2) | 불만 세 줄, 감독이 5샷 수정 후 재촬영 | 약 40분 | 약 ¥2.5 |
| 2026-11-11 | 숏드라마 『晾衣绳』(빨랫줄) | 4구간 11샷, 캐릭터 2명, 완성본 40.0초 | — | 약 ¥5.9(5회 유휴 대기 약 44분 포함) |
| 2026-11-12 | 댄스 리메이크 | 시험 촬영 4.5초 × 3테이크 | GPU 한 번에 약 19분 | 약 ¥1.82(유휴 대기 10분 포함) |

<sub>매번 적용되는 실제 단가는 GPU 가동 확인창이 플랫폼에서 실시간으로 조회한 값을 따릅니다.</sub>

**알려진 한계:**
- 클로즈업 첫 프레임이 참고 이미지에 끌려 미디엄 샷이나 바스트 샷이 되기 쉽고, 소품이 영상에서 다른 모양으로 바뀝니다(『晾衣绳』(빨랫줄)의 귤 봉지가 검은 가방이 됨).
- 머신 콜드 스타트에 2~3분이 걸리고, 가동 전 견적이 실제보다 높게 나올 때가 있습니다.

</details>

## 사용법

<details>
<summary><b>1. 첫 숏드라마 찍기</b></summary>

1. **대본 쓰기(무료)**: 「새 프로젝트」 → 「숏드라마」에서 제목과 한 줄 이야기를 적고 「감독에게 대본 쓰기 맡기기(무료)」를 누릅니다. 약 7~10분 걸립니다. 연습용 대본을 바로 복사해도 됩니다.
2. **설정 이미지 그리기(GPU)**: 하단 바의 가격이 적힌 버튼을 누르면 확인창에 GPU, 가용 영역, 단가, 예상 비용이 나옵니다. 「GPU 켜고 촬영」을 누릅니다.
3. **이미지 검토(로컬)**: 배우, 장면, 소품을 한 장씩 「승인」하거나, 이유를 한 줄 적어 「반려」합니다. 감독이 그 이유대로 고쳐서 다시 그립니다.
4. **첫 프레임 검토(로컬)**: 구간(약 10초)마다 첫 프레임이 한 장씩 있습니다. 구도, 외모와 나이, 의상과 소품, 어려 보일 때의 제한 사항, 이 네 가지 안내를 대조한 뒤 직접 「승인」을 누릅니다.
5. **영상 촬영과 테이크 선택(GPU)**: MiniMax H3(영상 생성 모델)가 한 번에 한 구간씩 찍습니다. 구간마다 여러 테이크 중 하나를 고릅니다(숫자 키 `1` `2`, `⌘Z`로 실행 취소).
6. **합성(무료)**: 로컬에서 음량을 맞추고 소프트 자막을 입혀 MP4로 내려받습니다. 화면에 워터마크는 없고, MP4 메타데이터에 AI 생성 표시가 기록됩니다.

</details>

<details>
<summary><b>2. 감독과 대화하기</b></summary>

<p align="center"><img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/mention-light.jpg" width="800" alt="『晾衣绳』(빨랫줄) 프로젝트 라이트 모드 화면: 왼쪽은 완성본 플레이어, 오른쪽 감독 입력창에 @를 입력해 배우, 장면, 소품, 구간 목록을 띄운 모습"></p>

- 프로젝트마다 감독 세션이 하나씩 있습니다. 대화에는 GPU 비용이 들지 않으며, 감독은 화면 언어로 답합니다.
- `@`로 배우, 장면, 소품, 특정 구간이나 영상 테이크를 지정할 수 있고, 다른 프로젝트나 에셋 라이브러리의 이미지도 가져올 수 있습니다.
- 화면을 클릭하거나 박스를 그리면 표시한 부분이 전체 프레임과 함께 감독에게 전달됩니다.
- 감독이 작업 중일 때 보낸 메시지는 대기열에 들어가며 「되돌리기」나 「끼어들기」를 할 수 있습니다. 「멈추기」는 감독만 멈추고 GPU는 멈추지 않습니다.

> 『末班车 v2』(막차 v2) 실제 테스트: 불만 세 줄만 적었더니 감독이 직접 프레임을 뽑아 원인을 찾고, 5샷을 고치고, 견적을 내고, 승인을 기다려 다시 찍고, 테이크를 골라 합성까지 마쳤습니다. 약 40분, ¥2.5.

</details>

<details>
<summary><b>3. 세 가지 재작업 도구</b></summary>

<p align="center"><img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/crop-light.jpg" width="800" alt="라이트 모드의 구간 B 클로즈업 자르기 대화상자: 첫 프레임 위에 2.0배 9:16 프레임을 드래그해 두었고, 아래에 다음 촬영 약 ¥0.41과 이번 단계는 과금되지 않는다는 안내가 표시됨"></p>

- **클로즈업 자르기**: 인물이 너무 멀리 잡혔을 때 첫 프레임에서 영역을 지정하면 선명한 클로즈업으로 다시 그립니다(최대 6배).
- **두 사람 옆얼굴 마주 보기**: 서로 바라봐야 할 두 사람이 모두 고개를 돌려 카메라를 보는 문제를 고칩니다.
- **의상 교체**: 에셋 라이브러리나 직접 올린 옷 이미지를 배우 사진 위로 끌어다 놓으면 다시 그립니다.

세 가지 모두 프로젝트 데이터만 바꾸며, 다음에 촬영을 눌러야 GPU가 켜집니다. 2026-11-10 실기기에서 검증했습니다(약 7분 / ¥0.43).

</details>

<details>
<summary><b>4. 레퍼런스 영상: 연출만 배우기</b></summary>

홈 화면 이야기 입력창 옆의 「레퍼런스」를 누르고 로컬 영상(최대 3분, `ffmpeg` 4.4+ 필요)을 고릅니다. 로컬에서 컷을 나누고 프레임을 뽑아 콘택트 시트로 합치면, 비전 모델이 정해진 선택지 안에서 샷마다 샷 크기, 카메라 워크, 조명, 인원수를 채우고, 이를 구간 단위 뼈대로 묶어 감독에게 넘깁니다. 원본의 인물, 외모, 대사, 화면은 프로젝트에 들어오지 않으며 원본 영상은 업로드하지 않습니다.

> 실제 테스트: 18초 레퍼런스 영상을 5샷으로 나눴고, 감독이 쓴 5샷은 샷 크기와 카메라 워크가 샷 단위로 맞아떨어졌습니다. 약 7분, GPU 비용 없음.

</details>

<details>
<summary><b>5. 리메이크</b></summary>

홈 화면에서 「리메이크」로 전환하고 댄스 영상(2~15초 구간을 고르거나 최대 5분 전체), 성인 캐릭터의 전신 정면 이미지와 얼굴 클로즈업(AI 생성이든, 쓸 권리가 있는 실제 인물이든), 의상 설명 한 줄을 올립니다. 가동을 승인하면 SAM 3D Body 모션 캡처, Blender 클레이 모델, MiniMax H3 + 캐릭터 교체 LoRA를 차례로 돌려 3단 비교 영상을 출력합니다. 마음에 들지 않으면 테이크를 더 찍거나 의상을 바꿔 다시 찍을 수 있고, 모션 캡처와 클레이 모델은 그대로 재사용합니다. 전체를 고르면 먼저 앞 4.5초를 몇 테이크 시험 촬영하고, 하나를 골라 「N번째로 전체 만들기」를 누르면 나머지를 몇 부분으로 나눠 찍어 한 편으로 잇습니다(25초 댄스에 약 1시간, ¥4~5. 시험 촬영 비용도 한도에 포함됩니다). 동작과 얼굴은 고른 테이크를 따르지만 의상 세부는 조금 달라질 수 있습니다.

</details>

## 작동 원리

1. **로컬 감독이 연출을 짭니다**: `dsh`에 설정한 LLM이 대본을 쓰고, 레퍼런스 영상의 뼈대를 나누고, 반려 이유에 맞춰 콘티를 고칩니다. 모든 프로젝트 파일은 로컬 `~/.dsh/shisei/jobs/`에 저장됩니다.
2. **내가 승인해야 GPU가 켜집니다**: 화면을 생성할 때마다 먼저 예상 비용을 계산하고, 「GPU 켜고 촬영」을 눌러야 플러그인이 [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) API로 내 4090 48G 인스턴스를 켜고 플랫폼 측 예약 종료를 설정합니다.
3. **터널로 클라우드 워크플로를 호출합니다**: `ssh` 터널로 클라우드에서 `127.0.0.1`만 리스닝하는 ComfyUI(클라우드 워크플로 서비스)에 접속해 Qwen-Image(이미지 생성 모델), SAM 3D Body, Blender, MiniMax H3를 돌립니다.
4. **로컬로 가져와 고르고 합성합니다**: 생성된 이미지와 영상을 로컬로 가져와 검토하고 테이크를 고르면, 로컬 `ffmpeg`로 음량을 맞추고 소프트 자막을 입혀 워터마크 없는 MP4를 내보냅니다. 클라우드는 10분간 유휴 상태면 자동으로 꺼집니다.

## 비용, 안전, 개인정보

아래 규칙은 코드에 들어 있으며 끌 수 없습니다.

- **GPU는 내가 눌러야 켜짐**: 「GPU 켜고 촬영」을 누르지 않으면 절대 켜지지 않으며, 감독의 셸에서 GPU를 켜 과금을 일으키는 명령은 모두 거부됩니다. 프로젝트마다 기본 예산 한도는 ¥5이고, 넘으면 켜지 않습니다.
- **화면의 추정치는 청구서보다 낮음**: 한도는 플러그인이 기록한 가동 시간으로 확인합니다. 하지만 촬영이 끝난 뒤 머신은 10분 동안 유휴 상태로 있다가 자동으로 꺼지고, 이 시간에도 플랫폼 요금이 나갑니다. 쉬엄쉬엄 나눠 찍으면 차이가 크게 벌어집니다(위의 『晾衣绳』(빨랫줄) 참고). 실제 금액은 [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) 청구서를 따릅니다.
- **전원만 끄고 아무것도 지우지 않음**: 플러그인은 전원만 끌 뿐 인스턴스와 디스크를 삭제하지 않으며, 교체된 첫 프레임과 영상도 이전 버전으로 남겨 둡니다.
- **캐릭터는 18세 이상**: 실제 인물 사진이나 실존 인물(유명인 포함)을 본뜬 이미지도 참고로 쓸 수 있어요. 플러그인은 검사하지도 막지도 않으며, 본인 동의를 받고 현지 초상권과 딥합성 표시 규정을 지키는 것은 사용자의 몫이에요. 「어려 보임」으로 표시한 캐릭터는 원래 체형만 쓰고 노출이 더 많은 옷으로 바꾸지 않으며, 치마 밑을 찍지 않고 로우 앵글은 무릎 높이보다 낮아지지 않습니다.
- **ComfyUI는 공개 인터넷에 노출되지 않음**: 클라우드에서 `127.0.0.1`만 리스닝하고 `ssh` 터널로 접속합니다. 가동할 때마다 외부에서 포트가 닫혀 있는지 확인하고, 대기열에 Shisei가 넣지 않은 작업이 나타나면 경고하고 일시 정지합니다. 키는 `dsh` 자격 증명 저장소나 환경 변수에만 두며, 파일로 저장하거나 화면에 다시 표시하지 않습니다.
- **데이터가 가는 곳이 투명함**: 텔레메트리도, Shisei 자체 서버도 없습니다. 데이터는 내가 설정한 곳으로만 갑니다. [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) API, 내 GPU 인스턴스(`ssh` 터널 경유), `dsh`의 감독 모델, 비전 모델(키를 설정한 경우에만 첫 프레임, 추출 프레임, 레퍼런스 콘택트 시트를 보내며 레퍼런스 원본 영상은 로컬 밖으로 나가지 않음), 그리고 읽기 전용 Shisei 에셋 라이브러리(상대 서버에 IP가 보임)입니다. 컴퓨터에 ffmpeg가 없으면 dsh에 설정한 npm 레지스트리에서 플러그인의 ffmpeg 패키지를 한 번 내려받습니다. 비용은 GPU 초당 요금과 감독 모델, 비전 모델 각각의 API 요금뿐입니다. 자세한 내용은 [PRIVACY.md](PRIVACY.md)를 참고하세요.

<details>
<summary><b>끄는 걸 깜빡해도 괜찮게: 예약 종료, 머신 페이지, 임대</b></summary>

- 작업 중에는 플러그인이 5분마다 [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) 플랫폼 측 예약 종료를 30분 뒤로 미루고, 작업이 끝나면 10분 뒤 종료로 바꿉니다. 플러그인이 꺼져 있어도 작동합니다.
- 왼쪽 바에 켜져 있는 머신 수, 시간당 요금, 사용 중인 프로젝트가 항상 표시됩니다. `dsh`가 켜져 있으면 3분마다 점검하는데, 점검은 예약 종료를 채울 뿐 직접 전원을 끄지 않습니다.
- 여러 대의 머신을 등록할 수 있고(「시작」이나 「머신」 페이지에서 한 대 더 만들 수 있습니다), 실행할 때마다 먼저 임대를 확보하므로 남의 머신을 끄지 않습니다. 모두 바쁘면 대기열에 넣을 수 있습니다. 먼저 비는 머신을 확인한 가격으로 쓰고, 기다리는 동안은 과금되지 않으며 최대 90분까지 기다립니다.

</details>

## 자주 묻는 질문

<details>
<summary><b>온라인 AI 영상 서비스와 무엇이 다른가요?</b></summary>

온라인 AI 영상 서비스는 웹 페이지만 열면 바로 쓸 수 있습니다. `dsh`나 클라우드 GPU 머신을 직접 설정할 필요도 없고, 2~3분의 머신 콜드 스타트를 기다릴 필요도 없습니다.

Shisei는 월 구독료를 내고 싶지 않고, 프로젝트 파일과 이전 버전을 모두 내 컴퓨터에 두고 싶은 사람에게 맞습니다. 화면을 생성하는 몇 분간의 클라우드 GPU 비용만 초 단위로 내고, 완성본에는 워터마크가 없으며, 코드는 오픈소스입니다. 화면에 동그라미를 그려 감독에게 마음에 안 드는 구간만 다시 찍게 할 수도 있습니다.

</details>

<details>
<summary><b>비용은 얼마나 드나요?</b></summary>

플러그인은 무료입니다. 비용은 두 곳에서만 나갑니다. GPU 초당 요금(위의 테스트 기록 참고), 그리고 감독 모델과 비전 모델 각각의 API 요금입니다(추천하는 Ling-3.2-Flash는 무료 제공량이 있습니다).

</details>

<details>
<summary><b>실제 인물 사진이나 연예인 스틸컷을 써도 되나요?</b></summary>

네. 캐릭터 참고는 실제 인물(유명인 포함)이어도 되고, 플러그인은 막지 않아요. 본인 동의와 현지 초상권·딥합성 표시 규정을 지키는 것은 사용자의 몫이에요. 완성 영상에는 워터마크를 넣지 않고, AI 생성 표시는 파일 메타데이터에만 기록해요. 업로드 창구는 장면, 소품, 의상만 받아요(의상 교체 경로가 옷만 다시 그리기 때문). 인물 사진은 프로젝트에 참고로 넣으면 됩니다.

</details>

<details>
<summary><b>어떤 그래픽카드와 운영체제를 지원하나요?</b></summary>

그래픽카드는 4090 48G를 씁니다. 다른 카드는 실기기에서 `gpu-qualify`를 통과하는 대로 추가됩니다. 운영체제는 macOS와 Windows를 지원합니다. Windows 11에서는 템플릿 복사, 감독의 대본 작성부터 완성본까지 숏드라마 전 과정을 끝까지 돌려 봤습니다(중국어판 Windows 기본 GBK 인코딩 포함). 리메이크, 「시작」 페이지의 머신 만들기, 「환경 업데이트」는 아직 Windows에서 검증하지 않았습니다. Linux는 로드맵에 있습니다.

</details>

<details>
<summary><b>플러그인을 업데이트하면 머신 환경을 다시 깔아야 하나요?</b></summary>

꼭 그렇지는 않습니다. 머신에는 별도의 환경 버전 번호가 있고, 설치할 것이 바뀔 때만 하나씩 올라갑니다. 맞지 않으면 감독이 알려 주고 「머신」 페이지에도 그 머신에 표시가 붙습니다. 머신이 꺼진 뒤 「환경 업데이트…」를 누르면, 가격을 확인한 뒤 플러그인이 머신을 켜서 [`setup.sh`](skills/shisei-motion/assets/image/setup.sh)를 다시 실행하고(바뀐 부분만 채움) 보안 점검 뒤 다시 끕니다. 규칙은 [docs/IMAGE.md](docs/IMAGE.md)에 있습니다.

</details>

<details>
<summary><b>상업적으로 써도 되나요?</b></summary>

플러그인 코드는 됩니다. 생성에 쓰는 모델은 각자 라이선스가 있으며, MiniMax H3와 캐릭터 교체 LoRA는 `minimax-h3-community-license`입니다. 상업적으로 쓰기 전에 먼저 읽어 주세요(아래 「라이선스」 참고).

</details>

<details>
<summary><b>문제가 생겼을 때</b></summary>

| 상황 | 해결 방법 |
| :--- | :--- |
| 키를 입력하지 않음 / GPU 머신을 고르지 않음 / ssh 키가 없음 | 「시작」 페이지의 해당 단계(ssh 키는 거기서 「ssh 키 만들기」)나 「설정」으로 갑니다 |
| [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) 커맨드라인 도구가 설치되지 않음 | 「설정」 → 「compshare-cli 설치」(플러그인 자체 디렉터리에 설치) |
| 감독이 대본을 완성하지 못함 / 오래 반응이 없음 | 감독 패널 위쪽에서 모델을 바꾸거나(마지막 메시지를 다시 보냅니다), `dsh`에서 정상 작동하는 모델을 설정한 뒤 다시 시도합니다 |
| 프로젝트 한도 초과 | 테이크나 구간을 줄이거나 「한도 올리기」를 누릅니다 |
| GPU 재고 없음 | 플러그인이 2분마다 자동으로 다시 시도하며, 기다리는 동안은 과금되지 않습니다 |
| 머신에 Shisei가 넣지 않은 작업이 나타남 | 플러그인이 대기열을 일시 정지했습니다. 「머신」 페이지에서 전원을 끄고 콘솔에서 인스턴스 보안을 점검하세요 |
| 머신 환경 버전이 낮거나 맞지 않음 | 머신이 꺼진 뒤 「머신」 페이지에서 「환경 업데이트…」를 누릅니다. 「플러그인보다 새 버전」이라고 나오면 플러그인을 업데이트하세요 |
| 다른 프로젝트가 머신을 쓰는 중 | 「대기했다가 비면 촬영」으로 빌 때까지 기다리거나 「시작」에서 한 대 더 만듭니다. 「머신」 페이지에서 누가 쓰는지 볼 수 있습니다 |
| 사전 점검 결과가 계속 안 나옴 | 「설정」에서 비전 모델을 테스트해 보세요. 사전 점검은 제안일 뿐이라 직접 승인하는 데는 지장이 없습니다 |

</details>

## 로드맵

- **내 GPU 사용**: 로컬 그래픽카드와 vast.ai.
- **더 많은 교체 가능한 모델**: 이미지 생성과 영상 생성 모델을 모두 바꿀 수 있게.
- **화면 비율과 배포 규격**: 가로형, 정사각형 등 더 많은 화면 비율과 배포 규격.
- **그 밖에**: 더 많은 그래픽카드, Linux, whisper 자막 타이밍 정렬, 다른 언어 대본, Shisei Pose(포즈 라이브러리, 선화를 클레이 모델 / 깊이 맵 / 노멀 맵으로 변환).

## 커뮤니티 · 감사의 말

- **커뮤니티**: [X @wquguru](https://x.com/wquguru) · [GitHub Issues](https://github.com/wquguru/dsh-shisei/issues) · 보안 문제 비공개 제보는 [SECURITY.md](SECURITY.md) · 개발 참여는 [CONTRIBUTING.md](CONTRIBUTING.md)
- **감사의 말**: [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness), [rea-video](https://github.com/JuneYaooo/rea-video)(레퍼런스 영상을 샷 단위로 분해하는 아이디어), ComfyUI, [ComfyUI-BFSNodes](https://github.com/alisson-anjos/ComfyUI-BFSNodes), Qwen-Image, MiniMax H3, SAM 3D Body, SAM 3.1, MoGe, Blender, whisper.cpp, ffmpeg.

## 라이선스

- 플러그인 코드는 Apache-2.0입니다([LICENSE](LICENSE)).
- 예외: `skills/shisei-motion/assets/workflows/` 아래의 `swap_studio.json`(ComfyUI-BFSNodes의 캐릭터 교체 예제를 수정), `drama_take.json`(그 예제의 MiniMax H3 샘플링 체인을 사용), `mocap_glb.json`은 **GPL-3.0-only**로 배포됩니다([NOTICE](NOTICE), [LICENSES/GPL-3.0.txt](LICENSES/GPL-3.0.txt)). 같은 디렉터리의 `drama_image.json`은 Apache-2.0입니다.
- `skills/shisei-motion/assets/vendor/wheels/`에는 compshare-cli(Apache-2.0)와 그 의존 패키지(MIT, BSD, ISC, Apache-2.0, MPL-2.0 등)를 수정 없이 넣어 두었습니다. 네트워크 없이 설치하기 위해서이며, 각 라이선스는 [NOTICE](NOTICE)에 있습니다.
- `lib/ssh.cjs`는 플러그인에 들어 있는 ssh입니다(컴퓨터에 OpenSSH가 없어도 돼요). ssh2(MIT)와 작은 라이브러리 몇 개(MIT, BSD-3-Clause, Unlicense)가 들어 있으며 [NOTICE](NOTICE)에 적혀 있습니다.
- 에셋 라이브러리는 플러그인과 함께 배포되지 않습니다. 플러그인이 `shisei.indaloop.app`의 공개 에셋을 온라인으로 불러오며, 그 안의 장면, 소품, 의상은 CC0입니다.
- 모델은 플러그인과 함께 배포되지 않으며, 실행할 때 내 인스턴스에서 받아옵니다. MiniMax H3와 캐릭터 교체 LoRA는 `minimax-h3-community-license`이고, Qwen-Image, SAM 3D Body, SAM 3.1, MoGe도 각자 라이선스가 있습니다. 상업적으로 쓰기 전에 먼저 읽어 주세요.
- 이 프로젝트는 독립적인 오픈소스 프로젝트이며 DeepSeek, [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc)(优云智算), MiniMax와 아무 관련이 없습니다.
