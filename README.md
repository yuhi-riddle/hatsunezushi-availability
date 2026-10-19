# 初音鮨の空席通知

初音鮨の予約ページを毎時17分（日本時間）に確認し、2名で予約できる土日のランチ枠をntfyに通知します。対象期間は実行日から予約受付の終わりまで（最長約1年先）で、毎日先へ進みます。枠の情報がない週が2週続いた時点を受付の終わりとみなし、それより先は取得しません。予約は通知のリンクから手動で申し込みます。

## 判定

人数2名の空席表から土日のランチ枠を探し、該当日の受付コースと開始時刻を照合します。ランチを提供するコースの空席を再確認してから通知します。日曜も受付が始まれば対象です。

コース名や価格は季節によって変わります。通知にはコース名を載せます。料金は予約ページで確認してください。

同じ枠の通知は、前回の空席状態と比較して省きます。一度埋まってから再び空いた枠は、再度通知します。予約ページへのアクセスが混雑（429）やサーバーエラー（5xx）、通信エラーで失敗した場合は、数秒おいて最大3回まで試します。確認に失敗した場合は前回の状態を保ちます。エラー通知は3回続けて失敗したときに1回だけ送り、成功すると数え直します。実行ログと `state.json` には、エラーの種類・ステータスコード・失敗した接続先（`tablecheck` / `github` / `ntfy`）だけを残します。

## 設定

GitHubリポジトリのSettings → Secrets and variables → Actionsで設定します。

| 種別 | 名前 | 内容 |
| --- | --- | --- |
| Secret | `NTFY_TOPIC` | Androidで購読しているトピック名 |
| Secret | `NTFY_SERVER` | ntfyサーバーURL。省略時は `https://ntfy.sh` |
| Secret | `NTFY_TOKEN` | 認証が必要なサーバーだけ設定 |
| Variable | `MONITOR_ENABLED` | `true` にすると監視と通知テストを実行可能 |

無料のntfy.shでは、トピック名を知っている人が通知を読み書きできます。推測されにくい専用トピックを使ってください。通知には予約枠とコース名だけを載せます。

## 起動前の確認

1. 保存先が公開リポジトリで、実行環境が標準の `ubuntu-latest` であることを確認する。この構成のActions実行時間は無料です。
2. Actions → Hatsunezushi availability → Run workflowで `dry_run` を選び、クラウドから空席を取得できるか確認する。
3. `MONITOR_ENABLED` を `true` にする。
4. Run workflowで `test_notification` を選び、Androidで通知を受信できるか確認する。
5. テスト後は毎時17分に定期実行される。停止するには `MONITOR_ENABLED` を `false` にする。

ジョブは1回2分で打ち切ります。実行結果の添付ファイルやキャッシュを保存せず、標準のGitHub実行環境を使います。非公開リポジトリや有料の実行環境へ変更する場合は料金を確認してください。

空席が変わらない場合も、週に1回確認週を保存します。公開リポジトリで長期間更新がない場合の定期実行の停止を防ぐためです。公開するデータは予約枠、確認週、エラー種別だけです。コミットの作成者には個人の氏名やメールアドレスを使いません。

定期実行は遅延・欠落する場合があります。1時間の確認間隔に開放されて埋まった枠は検出できません。サイトの仕様変更やアクセス制限によって監視が止まることがあります。

## 外部からの毎時起動

GitHubの定期実行（`schedule`）は混雑時に捨てられることがあり、2026年10月の実績では毎時の指定に対して1日5回前後しか動きませんでした。そこで無料のcron-job.orgから毎時、手動実行と同じ入口（`workflow_dispatch`）を呼び出します。`schedule` は外部サービスが止まったときの予備として残します。同じ時刻に両方が動いても、2回目は保存済みの状態と比べるため、同じ枠を重ねて通知しません。

### トークン（GitHub → Settings → Developer settings → Fine-grained tokens）

| 項目 | 値 |
| --- | --- |
| Repository access | Only select repositories → このリポジトリだけ |
| Repository permissions | Actions: Read and write（ほかは No access） |
| Expiration | 期限を設定し、切れる前に作り直す |

このトークンでできるのは、このリポジトリのActionsの実行・停止・ログ削除と、ワークフローの有効化・無効化までです。コードや設定は変更できません。

### cron-job.org のジョブ

| 項目 | 値 |
| --- | --- |
| URL | `https://api.github.com/repos/yuhi-riddle/hatsunezushi-availability/actions/workflows/monitor.yml/dispatches` |
| Schedule | 毎時17分、Time zone: Asia/Tokyo |
| Request method | POST |
| Headers | `Authorization: Bearer <トークン>` / `Accept: application/vnd.github+json` / `X-GitHub-Api-Version: 2022-11-28` / `Content-Type: application/json` |
| Request body | `{"ref":"main"}` |

成功すると応答は `204` になります。停止するには `MONITOR_ENABLED` を `false` にします（外部からの起動もジョブの条件で止まります）。

## ローカル確認

Python 3.10以上の標準ライブラリで動きます。

```text
python -m unittest -v
python monitor.py --dry-run
```

`--dry-run` は通知も状態の更新もせず、取得結果の件数だけを表示します。通知先はコードや実行ログへ出力しません。
