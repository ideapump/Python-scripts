from flask import Flask, render_template_string, request, send_from_directory
from flask_socketio import SocketIO, emit
import threading
import time

app = Flask(__name__)
socketio = SocketIO(app)

button_states = {"person1": False, "person2": False}

html = """
<!doctype html>
<html>
<head>
  <title>Practice Question AFAFP</title>
  <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
  <style>
    .ready-button {
      padding: 20px;
      font-size: 18px;
      width: 200px;
      margin: 10px;
    }
    .ready {
      background-color: #4CAF50;
      color: white;
    }
  </style>
</head>
<body>
  <h1>AFAFP</h1>
  <button id="person1" class="ready-button">Person 1 Ready</button>
  <button id="person2" class="ready-button">Person 2 Ready</button>
    <button id="person3" class="ready-button">Next Lesson</button>

  <audio id="click-sound" src="/static/click.mp3"></audio>

  <script>
    const socket = io();
    const sound = document.getElementById("click-sound");

    function updateButtons(states) {
      for (const [person, ready] of Object.entries(states)) {
        const btn = document.getElementById(person);
        if (ready) {
          btn.classList.add("ready");
        } else {
          btn.classList.remove("ready");
        }
      }
    }

    document.getElementById("person1").onclick = () => {
      socket.emit("button_click", "person1");
    };

    document.getElementById("person2").onclick = () => {
      socket.emit("button_click", "person2");
    };

    socket.on("update", (data) => {
      updateButtons(data);
    });

    socket.on("play_sound", () => {
      sound.currentTime = 0;
      sound.play();
    });
  </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(html)

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory('static', filename)

@socketio.on("button_click")
def handle_click(person):
    if person in button_states:
        button_states[person] = True

    socketio.emit("update", button_states)
    socketio.emit("play_sound")

    if all(button_states.values()):
        def reset_soon():
            time.sleep(0.5)
            for key in button_states:
                button_states[key] = False
            socketio.emit("update", button_states)
        threading.Thread(target=reset_soon).start()

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5001, debug=True)
