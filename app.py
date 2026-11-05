import os
from flask import Flask, render_template, session, request, redirect
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_KEY')

@app.route('/')
def home():    
    return render_template("sites/Main.html")

@app.route('/login')
def login():
    return render_template("sites/Login.html")

@app.route('/loginCheck', methods=["POST"])
def loginCheck():
    email = request.form.get("email")
    password = request.form.get("password")
    if email == "Testing@email.com" and password == 123456:
        session['user'] = "Testing"

    return redirect('/')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect("/")

@app.route('/Designers')
def designers():
    return render_template("sites/Designers.html")

@app.route("/SignUp")
def signup():
    return render_template("sites/SignUp.html")

@app.route('/Projects')
def projects():
    return render_template('sites/Main.html')

@app.route('/PlatformManagers')
def platform_managers():
    return render_template("sites/PlatformManagers.html")

@app.route('/GenerateReport')
def generate_report():
    report_types = [
            "daily report",
            "weekly report",
            "monthly report"
        ]
    today_date = datetime.today().strftime('%Y-%m-%d')
    return render_template("sites/GenerateReport.html",report_types=report_types, default_date=today_date)

@app.route('/ExportReport')
def export_report():
    return render_template("sites/ExportReport.html")

@app.route('/About')
def about():
    return render_template('sites/Main.html')


if __name__ == "__main__":
    app.run(debug=True, port=5001)
