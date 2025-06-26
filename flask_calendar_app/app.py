import os
from flask import Flask, redirect, url_for, session, request, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import datetime # datetimeをインポート

# Helper function to convert credentials to a dictionary
def credentials_to_dict(credentials):
   return {'token': credentials.token,
           'refresh_token': credentials.refresh_token,
           'token_uri': credentials.token_uri,
           'client_id': credentials.client_id,
           'client_secret': credentials.client_secret,
           'scopes': credentials.scopes}

# .envファイルから環境変数を読み込む
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("FLASK_SECRET_KEY", "dev_secret_key") # 環境変数から秘密鍵を読み込む (なければデフォルト値)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///app.db") # 環境変数からデータベースURLを読み込む
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' # 未認証時のリダイレクト先

# Google OAuth設定
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
# ローカル開発用に http を許可 (本番環境では https にしてください)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
REDIRECT_URI = 'http://127.0.0.1:5000/callback' # Googleからのリダイレクト先

# ユーザーモデル (仮実装、後で詳細を定義)
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    google_id = db.Column(db.String(200), unique=True, nullable=False)
    name = db.Column(db.String(100))
    email = db.Column(db.String(100))
    # responses = db.relationship('EventResponse', backref='author', lazy='dynamic') # UserからEventResponseを参照

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# 参加可否のステータスを定義
STATUS_CHOICES = {
    'attending': '出席',
    'declined': '欠席',
    'tentative': '未定'
}

class EventResponse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(255), nullable=False, index=True) # Google Calendar Event ID
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    status = db.Column(db.String(50), nullable=False) # 'attending', 'declined', 'tentative'
    comment = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    user = db.relationship('User', backref=db.backref('responses', lazy='dynamic'))

    def __repr__(self):
        return f'<EventResponse {self.event_id} by {self.user.name} - {self.status}>'


@app.route('/')
def index():
    if current_user.is_authenticated:
        return f'Hello, {current_user.name}! <a href="/logout">Logout</a><br><a href="/calendar">View Calendar</a>'
    return 'You are not logged in. <a href="/login">Login with Google</a>'

@app.route('/login')
def login():
    flow = Flow.from_client_secrets_file(
        'client_secret.json', # このファイルはユーザーが別途配置する必要があります
        scopes=['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/userinfo.profile', 'https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/calendar.events.readonly'],
        redirect_uri=REDIRECT_URI
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    session['oauth_state'] = state # CSRF対策
    return redirect(authorization_url)

@app.route('/callback')
def callback():
    state = session.pop('oauth_state', '')
    flow = Flow.from_client_secrets_file(
        'client_secret.json',
        scopes=None, # スコープは認証URL生成時に設定済み
        state=state,
        redirect_uri=REDIRECT_URI
    )
    flow.fetch_token(authorization_response=request.url)

    credentials = flow.credentials
    # credentialsをセッションまたはデータベースに保存 (ここではセッションに一時保存)
    session['credentials'] = credentials_to_dict(credentials) # credentials_to_dictは後で定義

    # ユーザー情報を取得
    service = build('oauth2', 'v2', credentials=credentials)
    user_info = service.userinfo().get().execute()

    google_id = user_info.get('id')
    email = user_info.get('email')
    name = user_info.get('name')

    # ユーザーがDBに存在するか確認、なければ作成
    user = User.query.filter_by(google_id=google_id).first()
    if not user:
        user = User(google_id=google_id, name=name, email=email)
        db.session.add(user)
        db.session.commit()

    login_user(user)
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    logout_user()
    session.pop('credentials', None) # 認証情報もセッションから削除
    return redirect(url_for('index'))

# Google Calendar API連携 (仮のルート、後で詳細を実装)
from flask import render_template # render_template をインポート

# ... (他のimport文) ...

@app.route('/calendar')
def calendar_view():
    if not current_user.is_authenticated or 'credentials' not in session:
        return redirect(url_for('login'))

    try:
        credentials = Credentials(**session['credentials']) # セッションから認証情報を復元
        service = build('calendar', 'v3', credentials=credentials)

        # Calendar API呼び出し (直近10件のイベントを取得)
        now = datetime.datetime.utcnow().isoformat() + 'Z' # 'Z' indicates UTC time
        events_result = service.events().list(
            calendarId='primary', timeMin=now,
            maxResults=10, singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])

        if not events:
            print('No upcoming events found.')
            return render_template('calendar.html', events_data=None)

        events_data = []
        for event in events:
            event_id = event['id']
            responses = EventResponse.query.filter_by(event_id=event_id).all()
            total_responses = len(responses)
            attending_count = sum(1 for r in responses if r.status == 'attending')
            tentative_count = sum(1 for r in responses if r.status == 'tentative')
            # declined_count = total_responses - attending_count - tentative_count # 必要であれば

            events_data.append({
                'summary': event['summary'],
                'id': event_id,
                'start': event['start'],
                'end': event['end'],
                'htmlLink': event.get('htmlLink'), # Google Calendarへのリンク
                'total_responses': total_responses,
                'attending_count': attending_count,
                'tentative_count': tentative_count,
            })

        return render_template('calendar.html', events_data=events_data)

    except Exception as e:
        print(f"An error occurred: {e}")
        # エラーが発生した場合、再ログインを促すかエラーページを表示
        # ここでは簡略化のためログインページにリダイレクト
        return redirect(url_for('login'))

# Jinja2カスタムフィルタ: ISO日時文字列またはdatetimeオブジェクトをフォーマット
def format_datetime_filter(value, format_str='%Y-%m-%d %H:%M'): # format引数名を変更
    if not value:
        return ""

    if isinstance(value, datetime.datetime):
        # 既にdatetimeオブジェクトの場合
        dt_obj = value
    elif isinstance(value, str):
        # 文字列の場合、パースを試みる
        try:
            if value.endswith('Z'):
                dt_obj = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
            else:
                dt_obj = datetime.datetime.fromisoformat(value)
        except ValueError:
            return value # パース失敗時は元の値を返す
    else:
        # datetimeでも文字列でもない場合はそのまま返す
        return value

    # ローカルタイムゾーンに変換したい場合は、ここで変換処理を入れる (例: pytzライブラリ使用)
    # ここではUTCまたはオフセット付きの時刻をそのままフォーマットする
    return dt_obj.strftime(format_str)

app.jinja_env.filters['datetimeformat'] = format_datetime_filter

@app.route('/event/<event_id>/responses')
@login_required # ログイン必須にする場合は Flask-Login の login_required を使う
def response_list_view(event_id):
    if not current_user.is_authenticated: # Flask-Loginを使わない場合の簡易チェック
        return redirect(url_for('login'))

    # TODO: event_idに対応するカレンダーイベント自体の情報をGoogle Calendar APIから取得して表示する方が親切
    # ここではまず、そのイベントIDに対するDB内の応答のみを取得・表示する

    responses = EventResponse.query.filter_by(event_id=event_id).order_by(EventResponse.updated_at.desc()).all()

    # イベントのサマリーを取得（もしあれば）
    # これは仮の処理で、実際にはカレンダーAPIを叩くか、イベント情報をDBに保存しておく必要がある
    event_summary = f"Responses for Event ID: {event_id}" # 仮のサマリー
    # credentials = session.get('credentials')
    # if credentials:
    #     try:
    #         creds = Credentials(**credentials)
    #         service = build('calendar', 'v3', credentials=creds)
    #         event_details = service.events().get(calendarId='primary', eventId=event_id).execute()
    #         event_summary = event_details.get('summary', event_summary)
    #     except Exception as e:
    #         print(f"Could not fetch event details for {event_id}: {e}")


    return render_template('response_list.html', responses=responses, event_id=event_id, event_summary=event_summary, STATUS_CHOICES=STATUS_CHOICES)

@app.route('/event/<event_id>/respond', methods=['GET', 'POST'])
@login_required
def response_form_view(event_id):
    # イベントのサマリーを取得 (response_list_viewと同様に改善の余地あり)
    event_summary = f"Response for Event ID: {event_id}"
    # ここで実際にGoogle Calendar APIからイベント詳細を取得する処理を入れるとより良い
    # 例:
    # credentials = session.get('credentials')
    # if credentials:
    #     try:
    #         creds = Credentials(**credentials)
    #         service = build('calendar', 'v3', credentials=creds)
    #         event_details = service.events().get(calendarId='primary', eventId=event_id).execute()
    #         event_summary = event_details.get('summary', event_summary)
    #     except Exception as e:
    #         print(f"Could not fetch event details for {event_id}: {e}")
            # エラー時はIDのみ表示するなどフォールバック

    # 既存の自分の投稿を探す
    existing_response = EventResponse.query.filter_by(event_id=event_id, user_id=current_user.id).first()

    if request.method == 'POST':
        status = request.form.get('status')
        comment = request.form.get('comment')

        if not status:
            # flash('Status is required.', 'error') # Flaskのflashメッセージを使う場合
            # return redirect(url_for('response_form_view', event_id=event_id))
            return "Status is required.", 400 # 簡単なエラー処理

        if status not in STATUS_CHOICES:
            return "Invalid status.", 400

        if existing_response:
            existing_response.status = status
            existing_response.comment = comment
            existing_response.updated_at = datetime.datetime.utcnow()
        else:
            new_response = EventResponse(
                event_id=event_id,
                user_id=current_user.id,
                status=status,
                comment=comment
            )
            db.session.add(new_response)

        try:
            db.session.commit()
            # flash('Response submitted successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            # flash(f'Error submitting response: {e}', 'error')
            print(f"DB commit error: {e}") # サーバーログにエラー出力
            return "Error submitting response.", 500

        return redirect(url_for('response_list_view', event_id=event_id))

    # GETリクエストの場合、フォームを表示
    return render_template('response_form.html',
                           event_id=event_id,
                           event_summary=event_summary,
                           status_choices=STATUS_CHOICES,
                           current_response=existing_response)


# データベース作成 (初回実行時など)
# from flask_sqlalchemy import inspect
# def create_tables():
#     with app.app_context():
#         inspector = inspect(db.engine)
#         if not inspector.has_table("user"): # Userテーブルが存在しない場合のみ作成
#             db.create_all()
#             print("Database tables created.")
#         else:
#             print("Database tables already exist.")

if __name__ == '__main__':
    with app.app_context(): # アプリケーションコンテキスト内でテーブル作成
        db.create_all() # 開発中は毎回テーブルを再作成する (本番ではマイグレーションツールを使用)
    app.run(debug=True, port=5000)
