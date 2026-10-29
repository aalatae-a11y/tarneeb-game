import os
import random
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret_key_tarneeb_libya_9988'
socketio = SocketIO(app, cors_allowed_origins="*")

rooms = {}

SUITS = ['hearts', 'spades', 'diamonds', 'clubs']
VALUES = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14] # 11=J, 12=Q, 13=K, 14=Ace

def create_deck():
    deck = [{'suit': s, 'val': v} for s in SUITS for v in VALUES]
    random.shuffle(deck)
    return deck

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join(data):
    room = data.get('room', 'main')
    name = data.get('name', 'لاعب')
    
    join_room(room)
    
    if room not in rooms:
        rooms[room] = {
            'players': [],
            'status': 'waiting', 
            'current_turn': 0,
            'dealer': 0,
            'trick': [],
            'scores': {},
            'bids': {},
            'tricks_won': {}
        }
    
    game = rooms[room]
    
    # إضافة اللاعب إذا لم يكن موجوداً مسبقاً
    if not any(p['id'] == request.sid for p in game['players']) and len(game['players']) < 4:
        game['players'].append({
            'id': request.sid,
            'name': name,
            'hand': [],
            'is_bot': False
        })
        game['scores'][request.sid] = 0
        game['tricks_won'][request.sid] = 0

    emit_game_state(room)

@socketio.on('fill_bots')
def fill_bots(data):
    room = data.get('room')
    if room not in rooms: return
    game = rooms[room]
    
    bot_names = ["حمودة (بوت)", "صلاح (بوت)", "مبروك (بوت)", "عطية (بوت)"]
    idx = 1
    while len(game['players']) < 4:
        bot_id = f"bot_{random.randint(1000,9999)}"
        game['players'].append({
            'id': bot_id,
            'name': bot_names.pop() if bot_names else f"بوت {idx}",
            'hand': [],
            'is_bot': True
        })
        game['scores'][bot_id] = 0
        game['tricks_won'][bot_id] = 0
        idx += 1
    
    start_new_round(room)

@socketio.on('start_game')
def start_game(data):
    fill_bots(data)

def start_new_round(room):
    game = rooms[room]
    game['status'] = 'bidding'
    deck = create_deck()
    
    for p in game['players']:
        p['hand'] = [deck.pop() for _ in range(12)]
        game['tricks_won'][p['id']] = 0
        game['bids'][p['id']] = 0
        
    emit_game_state(room)
    check_bot_turn(room)

@socketio.on('submit_bid')
def submit_bid(data):
    room = data.get('room')
    bid = int(data.get('bid', 2))
    sid = request.sid
    
    game = rooms[room]
    game['bids'][sid] = bid
    game['current_turn'] = (game['current_turn'] + 1) % 4
    
    if all(game['bids'].values()):
        game['status'] = 'playing'
        game['current_turn'] = (game['dealer'] + 1) % 4
        
    emit_game_state(room)
    check_bot_turn(room)

@socketio.on('play_card')
def play_card(data):
    room = data.get('room')
    card = data.get('card')
    sid = request.sid
    
    game = rooms[room]
    if game['status'] != 'playing': return
    
    player = next((p for p in game['players'] if p['id'] == sid), None)
    if player and card in player['hand']:
        player['hand'].remove(card)
        game['trick'].append({'player_id': sid, 'card': card})
        
        if len(game['trick']) == 4:
            winner_id = calculate_trick_winner(game['trick'])
            game['tricks_won'][winner_id] += 1
            game['trick'] = []
            
            if len(game['players'][0]['hand']) == 0:
                calculate_round_scores(room)
                start_new_round(room)
                return
            else:
                game['current_turn'] = next(i for i, p in enumerate(game['players']) if p['id'] == winner_id)
        else:
            game['current_turn'] = (game['current_turn'] + 1) % 4
            
        emit_game_state(room)
        check_bot_turn(room)

def calculate_trick_winner(trick):
    lead_suit = trick[0]['card']['suit']
    winning_card = trick[0]
    
    for play in trick[1:]:
        c = play['card']
        if c['suit'] == 'hearts' and winning_card['card']['suit'] != 'hearts':
            winning_card = play
        elif c['suit'] == winning_card['card']['suit'] and c['val'] > winning_card['card']['val']:
            winning_card = play
            
    return winning_card['player_id']

def calculate_round_scores(room):
    game = rooms[room]
    for p in game['players']:
        pid = p['id']
        bid = game['bids'].get(pid, 0)
        won = game['tricks_won'].get(pid, 0)
        
        if bid == 0:
            game['scores'][pid] += 5 if won == 0 else -5
        elif bid in [7]:
            game['scores'][pid] += 14 if won >= bid else -14
        elif bid in [9]:
            game['scores'][pid] += 21 if won >= bid else -21
        else:
            if won >= bid:
                game['scores'][pid] += bid
            else:
                game['scores'][pid] -= bid
                
        if game['scores'][pid] >= 31:
            game['status'] = 'ended'

def emit_game_state(room):
    if room not in rooms: return
    game = rooms[room]
    
    for p in game['players']:
        if not p['is_bot']:
            state = {
                'status': game['status'],
                'players': [{'name': pl['name'], 'id': pl['id'], 'cards_count': len(pl['hand']), 'score': game['scores'].get(pl['id'], 0)} for pl in game['players']],
                'current_turn': game['current_turn'],
                'dealer': game['dealer'],
                'trick': game['trick'],
                'my_hand': p['hand']
            }
            socketio.emit('game_update', state, room=p['id'])

def check_bot_turn(room):
    game = rooms[room]
    if game['status'] == 'ended': return
    current_p = game['players'][game['current_turn']]
    
    if current_p.get('is_bot'):
        socketio.start_background_task(run_bot_action, room, current_p['id'])

def run_bot_action(room, bot_id):
    socketio.sleep(1.5)
    if room not in rooms: return
    game = rooms[room]
    current_p = game['players'][game['current_turn']]
    if current_p['id'] != bot_id: return
    
    if game['status'] == 'bidding':
        bid = random.choice([2, 3, 4])
        game['bids'][bot_id] = bid
        game['current_turn'] = (game['current_turn'] + 1) % 4
        if all(game['bids'].values()):
            game['status'] = 'playing'
            game['current_turn'] = (game['dealer'] + 1) % 4
        emit_game_state(room)
        check_bot_turn(room)
    elif game['status'] == 'playing':
        hand = current_p['hand']
        if hand:
            card = hand.pop(0)
            game['trick'].append({'player_id': bot_id, 'card': card})
            if len(game['trick']) == 4:
                winner_id = calculate_trick_winner(game['trick'])
                game['tricks_won'][winner_id] += 1
                game['trick'] = []
                if len(game['players'][0]['hand']) == 0:
                    calculate_round_scores(room)
                    start_new_round(room)
                    return
                else:
                    game['current_turn'] = next(i for i, pl in enumerate(game['players']) if pl['id'] == winner_id)
            else:
                game['current_turn'] = (game['current_turn'] + 1) % 4
            emit_game_state(room)
            check_bot_turn(room)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
