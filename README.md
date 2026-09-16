# ssl_auto_streamer

RoboCup Small Size League (SSL) の試合をAIがリアルタイムで日本語音声実況するシステム。

SSL Vision TrackerとGame Controllerから試合データをUDPマルチキャストで受信し、Google Gemini Multimodal Live APIを通じて音声コメンタリーを自動生成・再生します。

## 主要機能

- **リアルタイム試合データ受信** - SSL Vision TrackerとGame ControllerからUDPマルチキャストで受信
- **ハイブリッドイベント検出** - GCのGroundTruthイベント（ゴール、ファール等）とトラッカーヒューリスティクス（パス、シュート、セーブ、ポゼッション変化）を組み合わせて検出
- **2モード実況** - 実況モード（Play-by-Play: 短く客観的）と解説モード（Color Commentary: 根拠重視の分析）を自動切替
- **Gemini Function Calling** - AIが試合データ（ゲーム状態、ボール軌跡、ロボット状態等）を自律的にクエリ
- **Web ダッシュボード** - フィールド可視化、イベントログ、設定変更、実況パイプラインの手動制御
- **OBS 配信オーバーレイ** - スコアボード等をブラウザソースとしてOBSに組み込み可能
- **チームプロファイル** - SSLチームの特徴・読み方・プレースタイルのデータベースを初期コンテキストとしてAIに提供

## アーキテクチャ

Statlerパターン（Writer/Reader分離）を採用しています。

```
SSL Vision Tracker (UDP 224.5.23.2:11010)
        |
        v
  TrackerClient ──> WorldModelWriter ──> WorldModelReader
                            |                    |
Game Controller (UDP 224.5.23.1:11003)          v
        |                            Gemini Live API (WebSocket)
        v                                        |
    GCClient ──> EventDetector                  v
                                          PcmAudioOutput
                                         (PyAudio 24kHz)
                                                 |
                                          スピーカー再生
```

**データフロー**:
1. UDP受信 → protobufデコード → WorldModelWriter（ゲーム状態更新）+ EventDetector（イベント検出）
2. イベント発生時 → WorldModelReader がリフレックス実況リクエスト生成 → Gemini送信
3. 静寂時（5秒以上）→ アナリストモードへ切替 → 解説リクエスト生成 → Gemini送信
4. Gemini → PCM音声データをWebSocketで返却 → PcmAudioOutputで再生
5. Web UI → WebSocket 5Hzでゲーム状態をブロードキャスト

## 必要条件

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (パッケージマネージャー)
- PortAudio（PyAudio用）
- Google Gemini API キー
- RoboCup SSL環境（SSL Vision Tracker + Game Controller）

## セットアップ

```bash
# リポジトリのクローン
git clone <repository-url>
cd ssl_auto_streamer

# 依存関係のインストール
make install
# または
uv sync --all-groups
```

## 実行方法

### ローカル実行

```bash
# 環境変数でAPIキーを渡す場合
export GEMINI_API_KEY=your_api_key
make run

# CLIオプションで指定する場合
uv run ssl-auto-streamer \
  --gemini-api-key YOUR_KEY \
  --our-team-color blue \
  --our-team-name ibis
```

**CLIオプション一覧**:

| オプション | デフォルト | 説明 |
|---|---|---|
| `--config` | `config/config.yaml` | 設定YAMLファイルパス |
| `--gemini-api-key` | - | Gemini APIキー（環境変数 `GEMINI_API_KEY` も可） |
| `--our-team-color` | `blue` | 自チームの色（`blue`/`yellow`） |
| `--our-team-name` | `ibis` | 自チーム名 |
| `--tracker-addr` | `224.5.23.2` | SSL Vision Trackerアドレス |
| `--tracker-port` | `11010` | SSL Vision Trackerポート |
| `--gc-addr` | `224.5.23.1` | Game Controllerアドレス |
| `--gc-port` | `11003` | Game Controllerポート |
| `--web-port` | `8080` | Web UIポート（`0`で無効化） |
| `--log-level` | `INFO` | ログレベル |

### Docker実行

```bash
docker run \
  -p 8080:8080 \
  --network host \
  -e GEMINI_API_KEY=your_api_key \
  ghcr.io/<owner>/ssl_auto_streamer:latest
```

> **注意**: UDPマルチキャスト受信には `--network host` が必要です。

## 設定

`config/config.yaml` で設定を管理します。

```yaml
gemini:
  api_key: ''              # 空文字の場合は GEMINI_API_KEY 環境変数から取得
  model: gemini-3.8-live   # 音声実況用 超低遅延Liveモデル
  output_transcription: true
  sample_rate: 24000       # 音声サンプルレート (Hz)

analysis_agent:
  enabled: true
  model: gemini-3.5-flash-lite  # 試合分析用 超高速モデル (350 tok/s)
  timeout_seconds: 5

ssl:
  tracker_addr: 224.5.23.2
  tracker_port: 11010
  gc_addr: 224.5.23.1
  gc_port: 11003
  our_team_color: blue     # blue / yellow
  our_team_name: ibis

commentary:
  mode: reflex_analyst     # 実況モード
  analyst_silence_threshold: 5  # 解説モードに切替えるまでの無音時間（秒）
  writer_update_rate: 1    # ワールドモデル更新レート (Hz)

audio:
  device: ''               # 空文字でデフォルト出力デバイスを使用
  output_mode: server      # server / client / both / off（Web UIから即時反映）
```

その他の設定ファイル:

| ファイル | 説明 |
|---|---|
| `config/team_profiles.yaml` | チームプロファイルデータベース |
| `config/ssl_rules.yaml` | SSLルール定義（ファール・セットプレー） |
| `config/tournament_context.yaml` | 大会状況・既出結果・トーナメント日程 |
| `config/system_instruction.md` | Geminiへのシステムプロンプト |
| `config/function_declarations.json` | Gemini Function Calling定義 |

## Web ダッシュボード

起動後、ブラウザで `http://localhost:8080` にアクセスします。

- フィールドのリアルタイム可視化（ボール・ロボット位置）
- イベントログ
- 実況パイプラインの手動開始/停止
- `audio.output_mode: client` / `both` 時のブラウザ音声再生（OBSオーバーレイ側でも再生）
- OBSオーバーレイへのフィールド表示切り替え
- チームカラー・チーム名の変更

**OBS配信オーバーレイ**: `http://localhost:8080/overlay.html` をOBSのブラウザソースに追加することで、スコアボード等を配信に重ねることができます。

## テスト・ログリプレイ

実際の試合ログ（SSL_LOG_FILE形式 / `.log` または `.log.gz`）を使用して動作確認やテストを実行できます。

### 1. 直接リプレイ実行（推奨）

UDP不要で、ログファイルから直接データを読み込んでWeb UIや実況パイプラインを動かせます。

```bash
# サンプル試合データ（tests/data/sample_match.log.gz）で等倍速再生
make replay

# または任意のログファイルを指定して実行
uv run ssl-auto-streamer --replay-log /path/to/match.log.gz --replay-speed 1.0
```

### 2. UDP送信ツール (`ssl-log-player`)

実運用さながらにUDPマルチキャストでログパケットを送信し、別プロセスで起動した `ssl-auto-streamer` で受信テストを行います。

```bash
# ターミナル1: アプリ起動
make run

# ターミナル2: ログパケット送信
make play-log
# または
uv run ssl-log-player tests/data/sample_match.log.gz --speed 1.0
```

### 3. ログ切り出しツール (`ssl-log-cutter`)

長時間の試合ログから、指定した秒数やパケット数で軽量なテスト用ログを切り出せます。

```bash
uv run ssl-log-cutter /path/to/full_match.log.gz cut_sample.log.gz --duration-sec 60.0
```

## 開発

```bash
# protobufスタブの再生成
make proto

# リンター
uv run ruff check

# テスト実行
make test

# ログリプレイテストのみ実行
make test-log
```

## ライセンス

Apache License 2.0
