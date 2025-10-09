import json
import uuid
import logging
import socketio
from fastapi import FastAPI

# -----------------------------------------------------
# setup logging
# -----------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("leetpal")

# -----------------------------------------------------
# socket.io + fastapi setup
# -----------------------------------------------------
# ⚠️ replace <YOUR_EXTENSION_ID> with your actual Chrome extension ID
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=["chrome-extension://<YOUR_EXTENSION_ID>"],
    ping_interval=25,      # seconds between heartbeats
    ping_timeout=10,       # drop dead sockets quickly
)
app = FastAPI()
asgi_app = socketio.ASGIApp(sio, app)

# -----------------------------------------------------
# in-memory state (fine for single instance)
# -----------------------------------------------------
waiting_queue = []           # users waiting for a partner
active_sessions = {}         # session_id -> [sid1, sid2]
messages = {}                # session_id -> list of messages

# -----------------------------------------------------
# helpers
# -----------------------------------------------------
async def create_session(sid_a, sid_b):
    session_id = f"session:{uuid.uuid4().hex[:8]}"
    active_sessions[session_id] = [sid_a, sid_b]
    messages[session_id] = []

    # notify both users
    await sio.emit("matched", {"session_id": session_id, "partner": sid_b[-4:]}, to=sid_a)
    await sio.emit("matched", {"session_id": session_id, "partner": sid_a[-4:]}, to=sid_b)
    log.info(f"[MATCHED] {sid_a[-4:]} ↔ {sid_b[-4:]} → {session_id}")
    return session_id


# -----------------------------------------------------
# socket events
# -----------------------------------------------------
@sio.event
async def connect(sid, environ, auth):
    client_id = auth.get("client_id") if auth else None
    log.info(f"[CONNECTED] {sid[-4:]} ({client_id})")

    # cleanup any stale state
    if sid in waiting_queue:
        waiting_queue.remove(sid)
    for session, sids in list(active_sessions.items()):
        if sid in sids:
            del active_sessions[session]

    await sio.emit("connected", {"user_id": f"User_{client_id or sid[-4:]}"}, to=sid)


@sio.event
async def find_partner(sid, data=None):
    log.info(f"[FIND_PARTNER] {sid[-4:]}")
    if sid in waiting_queue:
        waiting_queue.remove(sid)

    for sids in active_sessions.values():
        if sid in sids:
            log.info(f"[INFO] {sid[-4:]} already in active session, skipping.")
            return

    log.info(f"[QUEUE STATE] {waiting_queue}")

    if not waiting_queue:
        waiting_queue.append(sid)
        await sio.emit("waiting", {"message": "Waiting for a partner..."}, to=sid)
        log.info(f"[WAITING] {sid[-4:]}")
    else:
        partner_sid = waiting_queue.pop(0)
        await create_session(sid, partner_sid)


@sio.event
async def send_message(sid, data):
    session_id = data.get("session_id")
    msg_text = data.get("message")

    if session_id not in active_sessions:
        log.warning(f"[ERROR] Invalid session: {session_id}")
        return

    messages[session_id].append({"sender": sid[-4:], "text": msg_text})

    partner_sids = [s for s in active_sessions[session_id] if s != sid]
    for user_sid in partner_sids:
        await sio.emit(
            "new_message",
            {"sender": f"User_{sid[-4:]}", "message": msg_text},
            to=user_sid,
        )

    await sio.emit("message_ack", {"status": "delivered", "message": msg_text}, to=sid)
    log.info(f"[MSG] ({session_id}) User_{sid[-4:]}: {msg_text}")


@sio.event
async def disconnect(sid):
    log.info(f"[DISCONNECTED] {sid[-4:]}")
    if sid in waiting_queue:
        waiting_queue.remove(sid)

    for session_id, sids in list(active_sessions.items()):
        if sid in sids:
            active_sessions.pop(session_id, None)
            messages.pop(session_id, None)
            partner_sid = next((s for s in sids if s != sid), None)
            if partner_sid:
                await sio.emit("status", {"message": "Your partner disconnected."}, to=partner_sid)
            log.info(f"[CLEANUP] {sid[-4:]} removed from {session_id}")
            break


@sio.event
async def disconnect_manual(sid, data):
    """Manual disconnect from Chrome popup"""
    session_id = data.get("session_id")

    if not session_id:
        for sess, sids in list(active_sessions.items()):
            if sid in sids:
                session_id = sess
                break

    if not session_id:
        log.info(f"[DISCONNECT_MANUAL] {sid[-4:]} not in any session.")
        return

    if session_id in active_sessions:
        sids = active_sessions.pop(session_id, [])
        messages.pop(session_id, None)
        log.info(f"[MANUAL DISCONNECT] {sid[-4:]} left {session_id}")

        for partner_sid in sids:
            if partner_sid != sid:
                await sio.emit("status", {"message": "Your partner disconnected."}, to=partner_sid)
                log.info(f"[CLEANUP] Removed partner {partner_sid[-4:]} from {session_id}")
        log.info(f"[CLEANUP COMPLETE] {session_id} fully cleared.")
    else:
        log.warning(f"[WARN] Session {session_id} not found during manual disconnect.")


# -----------------------------------------------------
# run
# -----------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    # use 0.0.0.0 so Render/Fly.io can bind publicly
    uvicorn.run(asgi_app, host="0.0.0.0", port=8000)
