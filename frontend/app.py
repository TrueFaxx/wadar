from flask import Flask, render_template

app = Flask(__name__, template_folder='frontend/templates')

@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/wadar')
def wadar():
    return render_template('index.html')

if __name__ == '__main__':
<<<<<<< HEAD:app.py
    app.run(debug=True, port=5002)
=======
    app.run(debug=True)
>>>>>>> ff2cf76437a834004b187375ff2e54d08a34b8c5:frontend/app.py
