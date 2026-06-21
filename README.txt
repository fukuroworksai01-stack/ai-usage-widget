AI Usage Widget
===============
Claude Pro / ChatGPT Plus のサブスク利用枠（5時間枠・週枠）を、PC常駐の
小さなフローティング・ウィジェットでリアルタイム表示するツールです。
各枠の「残り%」と「リセットまでの時間」をバー表示します。


■ 同梱ファイル
  - ai_usage_widget.py … 本体スクリプト
  - build.bat          … EXE を作るためのビルド用バッチ（Windows）
  - icon.ico           … EXE 用アイコン
  - README.txt         … このファイル


■ EXE の作り方（Windows）
  1. 4ファイルを同じフォルダに置く。
  2. build.bat をダブルクリック。
     （Python が必要です。未導入なら https://www.python.org/downloads/ から
       入れ、インストーラで "Add Python to PATH" にチェック）
  3. 完了すると dist\AIUsageWidget.exe が出来ます。これを実行。

  ※ EXE はビルドした OS 用です（Windows で作れば Windows 用）。


■ EXE を使わず、そのまま動かす場合
     pip install requests
     python ai_usage_widget.py
  （コンソールを出したくないとき: pythonw ai_usage_widget.py）


■ 初期設定（取得元の資格情報）
  ChatGPT 側:
    Codex CLI でログイン済みなら ~/.codex/auth.json を自動で読みます。設定不要。

  Claude 側（sessionKey の入れ方は3通り）:
    ⚙（または右クリック →「設定…」）を開き、Claude欄で：
    (1) 「自動取得」… Firefox 等にログイン済みなら自動でsessionKeyを取得。
    (2) 「ログインして取得」… Chrome/Edge 推奨。専用ウィンドウが開くので
        claude.ai に一度ログインすると自動でsessionKeyを取り込みます
        （Chrome 127+ の暗号化を、ブラウザ自身に復号させて正規に回避）。
    (3) 手動貼り付け… claude.ai を開く → F12 → Application（または Storage）
        → Cookies → https://claude.ai → "sessionKey" の値をコピーして貼り付け。
    入れたら「接続テスト」で確認 → OKなら「保存」。
    ※ claude.ai は Cloudflare 配下のため、まれに standalone アプリからの取得が
      ブロックされることがあります。本ツールは curl_cffi（Chrome 互換の通信設定）が
      入っていればそれを使い、ブラウザに近い通信で取得を試します（build.bat が自動導入）。
      それでもダメな場合は、右クリック→「Claude接続をテスト…」で原因を確認するか、
      ブラウザ拡張をご利用ください。

  カスタムAI（Manus / Cursor / v0 などクレジット制AIを追加）:
    ⚙ →「カスタムAI」→「＋ AIを追加」で、以下を設定します。
      - 名前 / 色
      - 使用量API の URL … 対象サイトにログイン → F12 → Network タブを開き、
        残高や使用量が載っているリクエスト（例: .../api/credits 等）の URL をコピー
      - 認証方式 … 「ブラウザ自動」(対象ドメインにログイン済みのCookieを使用)が簡単。
        または Cookie貼付 / Bearerトークン / カスタムヘッダ
      - value_path … 応答JSON内の値の場所。例 data.credits.remaining
      - 種別 … 残クレジット / 使用率% / 残り%
      - total_path（任意・残量バーを出す総量）, reset_path（任意・リセット日時）
    「テスト」ボタンでその場で取得確認できます。
    ※ Gemini（Google）は認証(SAPISIDHASH等)が特殊で、この方法では基本取得できません。
      Cookie/Bearer で叩けるサービス（多くのクレジット制AI）が対象です。


■ 操作
  - 各枠は円形リングゲージで残量%（緑→黄→赤）を表示。複数枠は横並びでコンパクト。
    クレジット制は「残/総（リング）」または総量不明なら残クレジット数を表示します。
  - タイトルバーをドラッグで移動（位置は自動保存／画面外に出ても自動で戻す）
  - 右下のグリップをドラッグ、またはウィジェット上でマウスホイールでサイズ変更
    （拡大しても文字は鮮明＝ベクター再描画）。右クリック →「表示倍率」でも調整可
  - 右クリック →「Claude接続をテスト…」で原因（HTTP状態/Cloudflare/Cookie有無
    /使用エンジン）を表示。うまく繋がらない時はこれで切り分け
  - 右上 ⚙ で設定ダイアログ、⟳ で更新、✕ で終了
  - 取得に失敗しても直前に取れた値を「⚠ 更新失敗・HH:MM時点の値」として
    表示し続けます（毎回エラーで点滅しません）


■ 設定・ログの場所
  設定:   %USERPROFILE%\.ai-usage-widget\config.json
  エラー: %USERPROFILE%\.ai-usage-widget\error.log
  （起動しない時はこのログを確認）

  config.json の主な項目（基本は ⚙ から変更できます）:
    refresh_minutes    … 更新間隔（分）
    low_threshold_pct  … 残りがこの%を切ると赤く警告
    alert_sound        … しきい値を下回った時に音で知らせる（Windows）


■ 注意・限界（重要）
  - サブスク利用枠の「残り」を返す公式 API は存在しません。本ツールは各公式
    アプリが内部で使うエンドポイントを、ローカルの資格情報で取得します。
    各社が仕様を変えると動かなくなる可能性があります。
  - Claude は claude.ai の sessionKey（あなた自身のブラウザ相当のセッション）
    を使います。Claude Code の OAuth トークンを API に直接使う方式は規約違反
    かつアカウント停止リスクがあるため、本ツールでは使っていません。
  - sessionKey は config.json に平文で保存されます。自分の PC 内だけに
    とどめ、他人と共有しないでください。
