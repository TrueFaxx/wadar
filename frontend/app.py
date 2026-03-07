from flask import Flask, render_template

app = Flask(__name__)

@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/static/<path:path>')
def serve_static(path):
    return app.send_static_file(path)

@app.route('/wadar')
def wadar():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True, port=5002)
