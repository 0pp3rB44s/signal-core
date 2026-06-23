from flask import Flask, render_template

from api.routes import api

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static"
)

app.register_blueprint(api)


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5001,
        debug=True
    )