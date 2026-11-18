from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from agent import run_agent

app = Flask(__name__)
version = "0.1.0"
@app.route('/ping')
def ping():
    return "pong"

@app.route('/')
def index():
    answer = None
    q = request.args.get("q", "").strip()
    if q:
        answer, usage = run_agent(q)
    return render_template("index.html",title=f"问数 | AskDB | 版本号:{version}",q=q,answer=answer)

if __name__ == '__main__':
    app.run(host="127.0.0.1", port=5000, debug=True)