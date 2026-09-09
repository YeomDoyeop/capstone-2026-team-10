AVE 클라이언트
==============

AVE는 영상 분석 및 자동 편집을 지원하는 Windows용 클라이언트입니다.
이 배포물은 별도의 설치 과정 없이 압축을 푼 위치에서 실행합니다.


처음 실행하는 방법
------------------

1. ZIP 파일의 압축을 쓰기 가능한 폴더에 완전히 풉니다.
2. ave_updater.exe를 실행합니다.
3. 필요한 구성 요소를 선택하고 [다음]을 눌러 다운로드합니다.
4. 작업이 완료되면 ave_client.exe를 실행합니다.

ave_client.exe와 ave_updater.exe의 위치를 옮길 때에는 static 폴더도
같은 위치에 함께 두어야 합니다. ZIP 안의 디렉터리 구조를 유지하십시오.


포함 파일
---------

ave_client.exe
  AVE 클라이언트 실행 파일입니다. 실행하면 로컬 서버와 시스템 트레이가
  시작되고 기본 웹 브라우저에서 사용자 인터페이스가 열립니다.

ave_updater.exe
  AVE에 필요한 최신 FFmpeg, FFprobe 및 yt-dlp 바이너리를 다운로드합니다.
  AVE 클라이언트 자체는 변경하지 않습니다.

static\ui
  AVE의 웹 사용자 인터페이스 파일입니다. ave_client.exe가 실행 파일과
  같은 위치의 이 디렉터리를 읽으므로 삭제하거나 분리하지 마십시오.


로컬 생성 파일
--------------

AVE를 사용하면 실행 파일이 있는 위치에 다음 항목이 생성될 수 있습니다.

bin\       FFmpeg, FFprobe 및 yt-dlp 실행 파일
db\        로컬 작업 데이터베이스
media\     원본 자료, 작업 파일 및 편집 결과
client.log 클라이언트 실행 로그

업데이터는 다운로드 중에 .temp 폴더를 사용하며, 작업이 끝나면 업데이터가
생성한 임시 파일과 빈 .temp 폴더를 삭제합니다.


문제 해결
---------

* ave_client.exe 실행 전에 ave_updater.exe로 필요한 구성 요소를 받으십시오.
* 웹 화면이 열리지 않으면 static\ui\index.html이 있는지 확인하십시오.
* 오류 원인은 시스템 트레이의 [로그 열기] 또는 client.log에서 확인하십시오.
* Windows 보안 경고가 표시되면 파일을 받은 출처를 확인한 뒤 실행 여부를
  결정하십시오.
* AVE 폴더와 그 하위 파일에 쓰기 권한이 있어야 합니다.


외부 구성 요소 및 라이선스
--------------------------

다음 구성 요소는 AVE 배포 ZIP에 포함되지 않습니다. 사용자가 업데이터에서
선택하면 각 제공처에서 직접 다운로드되며, AVE와 별도 프로세스로 실행됩니다.

FFmpeg / FFprobe
  프로젝트: https://ffmpeg.org/
  Windows 빌드: https://www.gyan.dev/ffmpeg/builds/
  라이선스 안내: https://ffmpeg.org/legal.html

  현재 업데이터는 gyan.dev의 최신 Release Essentials 빌드를 사용합니다.
  해당 빌드에는 제공처가 명시한 GNU GPL 버전 3 조건이 적용됩니다.

yt-dlp
  프로젝트: https://github.com/yt-dlp/yt-dlp
  라이선스: https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE
  제3자 라이선스:
  https://github.com/yt-dlp/yt-dlp/blob/master/THIRD_PARTY_LICENSES.txt

각 구성 요소에는 해당 프로젝트와 빌드 제공처가 고지한 라이선스 조건이
독립적으로 적용됩니다. AVE는 위 외부 프로젝트의 공식 제품이 아닙니다.


사용 시 주의사항
---------------

AVE와 외부 구성 요소는 관련 법률, 영상의 저작권 및 이용하는 서비스의
약관을 준수하는 범위에서 사용해야 합니다. 다운로드하거나 편집할 콘텐츠에
필요한 권리가 있는지 확인할 책임은 사용자에게 있습니다.
