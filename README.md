# ssl_auto_streamer

RoboCup Small Size League (SSL) の試合をAIがリアルタイムで日本語音声実況するシステム。

SSL Vision TrackerとGame Controllerから試合データをUDPマルチキャストで受信し、Google Gemini Multimodal Live APIを通じて音声コメンタリーを自動生成・再生します。

## 主要機能

- **リアルタイム試合データ受信** - SSL Vision TrackerとGame ControllerからUDPマルチキャストで受信
- **ハイブリッドイベント検出** - GCのGroundTruthイベント（ゴール、ファール等）とトラッカーヒューリスティクス（パス、シュート、セーブ、ポゼッション変化）を組み合わせて検出
- **2モード実況** - 実況モード（Play-by-Play: 短く客観的）と解説モード（Color Commentary: 根拠重視の分析）を自動切替
- **Gemini Function Calling** - AIが試合データ（ゲーム状態、ボール軌跡、ロボット状態、フォーメーション等）を自律的にクエリ
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
  model: gemini-2.5-flash-native-audio-preview-12-2025
  sample_rate: 24000       # 音声サンプルレート (Hz)

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
```

その他の設定ファイル:

| ファイル | 説明 |
|---|---|
| `config/team_profiles.yaml` | チームプロファイルデータベース |
| `config/ssl_rules.yaml` | SSLルール定義（ファール・セットプレー） |
| `config/system_instruction.md` | Geminiへのシステムプロンプト |
| `config/function_declarations.json` | Gemini Function Calling定義 |

## Web ダッシュボード

起動後、ブラウザで `http://localhost:8080` にアクセスします。

- フィールドのリアルタイム可視化（ボール・ロボット位置）
- イベントログ
- 実況パイプラインの手動開始/停止
- チームカラー・チーム名の変更

**OBS配信オーバーレイ**: `http://localhost:8080/overlay.html` をOBSのブラウザソースに追加することで、スコアボード等を配信に重ねることができます。

## 開発

```bash
# protobufスタブの再生成
make proto

# リンター
uv run ruff check

# テスト
uv run pytest
```

## ライセンス

Apache License 2.0
