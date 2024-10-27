import os
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, session, url_for, jsonify
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import openai
import base64
from cachetools import TTLCache


load_dotenv()
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
openai.api_key = os.getenv("OPENAI_API_KEY")

# Set up OAuth 2.0 flow
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
flow = Flow.from_client_secrets_file("credentials.json", scopes=SCOPES)
flow.redirect_uri = "http://127.0.0.1:5000/oauth2callback"

# Cache setup
email_cache = TTLCache(maxsize=1000, ttl=86400)  # 24 hours cache to make load time faster

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'credentials' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function

@app.route("/")
def index():
    return render_template("login.html")

@app.route("/login")
def login():
    authorization_url, state = flow.authorization_url()
    session["state"] = state
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    flow.fetch_token(authorization_response=request.url)
    session["credentials"] = credentials_to_dict(flow.credentials)
    return redirect("/emails")

@app.route("/emails")
@login_required
def display_emails():
    page = request.args.get('page', 1, type=int)
    per_page = 10
    gmail = get_gmail_service()
    
    start_index = (page - 1) * per_page

    # this fetches the messages for the page 
    results = gmail.users().messages().list(userId="me", maxResults=per_page, pageToken=get_page_token(start_index)).execute()
    messages = results.get("messages", [])
    next_page_token = results.get("nextPageToken")

    emails = []
    for message in messages:
        email_id = message["id"]
        if email_id in email_cache:
            emails.append(email_cache[email_id])
        else:
            msg = gmail.users().messages().get(userId="me", id=email_id).execute()
            email_content = get_email_content(msg)
            summary = summarize_email(email_content)
            description = describe_email(email_content)
            category = categorize_email(email_content)
            sent_time = get_sent_time(msg)
            spooky = category in ["Work", "Education"]
            email_data = {
                "subject": summary,
                "description": description,
                "from": get_header(msg, "From"),
                "id": email_id,
                "category": category,
                "sent_time": sent_time,
                "spooky": spooky,
            }
            email_cache[email_id] = email_data
            emails.append(email_data)

    return render_template("emails.html", emails=emails, page=page, next_page_token=next_page_token)

def credentials_to_dict(credentials):
    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
    }

def get_gmail_service():
    credentials = Credentials(**session["credentials"])
    return build("gmail", "v1", credentials=credentials)

def get_header(message, name):
    return next((header["value"] for header in message["payload"]["headers"] if header["name"] == name), "")

def get_email_content(message):
    parts = message["payload"].get("parts", [])
    
    if parts:
        for part in parts:
            if part["mimeType"] == "text/plain":
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")[:4000]
    
    return base64.urlsafe_b64decode(message["payload"]["body"]["data"]).decode("utf-8")[:4000]

def summarize_email(content, max_tokens=30):
    instruction = "Summarize the main topic of this email in a casual, slightly funny way. Keep it under 10 words if possible, but include all important info. "
    content = content[:8000] + "..." if len(content) > 8000 else content

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": f"Email content:\n\n{content}"},
            ],
            max_tokens=max_tokens,
            temperature=0.8,
        )
        return response.choices[0].message["content"].strip()
    except openai.error.InvalidRequestError as e:
        print(f"Error summarizing email: {str(e)}")
        words = content.split()
        return 'I got an email saying: ' + ' '.join(words[:6]) + "..." if len(words) > 6 else ' '.join(words)

def describe_email(content, max_tokens=50):
    instruction = "Describe the email in one slightly funny sentence, it is okay if the sentence is not complete, as introductory words are redundant. Include all important details in a casual manner, leave out redundant information, and never end on an incomplete sentence."
    content = content[:8000] + "..." if len(content) > 8000 else content

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": f"Email content:\n\n{content}"},
            ],
            max_tokens=max_tokens,
            temperature=0.9,
        )
        return response.choices[0].message["content"].strip()
    except openai.error.InvalidRequestError as e:
        print(f"Error describing email: {str(e)}")
        return "Yo, I got this wild email. You gotta check it out!"

def categorize_email(content):
    instruction = "Categorize this email as exactly one of the following: 'Advertisement', 'Work', 'Entertainment', 'Education', or 'Personal'. Use only these exact words."
    content = content[:8000] + "..." if len(content) > 8000 else content

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": f"Email content:\n\n{content}"},
            ],
            max_tokens=15,
            temperature=0.3,
        )
        category = response.choices[0].message["content"].strip()
        

        valid_categories = ["Advertisement", "Work", "Entertainment", "Education", "Personal"]
        if category not in valid_categories:
            return "Personal"  
        
        return category
    except openai.error.InvalidRequestError as e:
        print(f"Error categorizing email: {str(e)}")
        return "Personal"  

def get_sent_time(message):
    sent_time = get_header(message, "Date")
    return sent_time

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

def get_page_token(start_index):
    if start_index == 0:
        return None
    
    gmail = get_gmail_service()
    results = gmail.users().messages().list(userId="me", maxResults=start_index).execute()
    return results.get("nextPageToken")

@app.route("/chat", methods=["GET", "POST"])
@login_required
def chat():
    if request.method == "GET":
        return render_template("chat.html")
    
    user_message = request.json.get("message")
    response = chat_about_emails(user_message)
    return jsonify({"response": response})

def chat_about_emails(user_message):
    gmail = get_gmail_service()
    
    results = gmail.users().messages().list(userId="me", maxResults=10).execute()
    messages = results.get("messages", [])
    
    email_descriptions = []
    for message in messages:
        email_id = message["id"]
        if email_id in email_cache:
            email_descriptions.append(email_cache[email_id]["description"])
        else:
            msg = gmail.users().messages().get(userId="me", id=email_id).execute()
            email_content = get_email_content(msg)
            description = describe_email(email_content)
            email_descriptions.append(description)
    
    email_context = "\n".join(f"- {description}" for description in email_descriptions)
    
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are an simplemail, you help users discuss their recent emails in a casual manner You have access to descriptions of their 10 most recent emails. Keep your responses very brief but to the point, recognize the importance of each email and respond accordingly."},
                {"role": "user", "content": f"Here are descriptions of my recent emails:\n{email_context}\n\nUser question: {user_message}"},
            ],
            max_tokens=150,
            temperature=0.7,
        )
        return response.choices[0].message["content"].strip()
    except openai.error.InvalidRequestError as e:
        print(f"Error in chat response: {str(e)}")
        return "I'm sorry, I'm having trouble processing your request right now. Can you try asking something else?"

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
