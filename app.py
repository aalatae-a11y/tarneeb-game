import os
import random
import time
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'tarneeb_libya_secret_key_2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# تخزين الغرف وحالة الألعاب في الذاكرة
ROOMS = {}

SUITS = ['Hearts', 'Tiles', 'Clovers', 'Pikes']  # Hearts = قلب أحمر (حاكم)
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
RANK_VALUES = {r: i for i, r in enumerate(RANKS, start=2)}

def create_deck():
    deck = []
    for s in SUITS:
        for r in RANKS:
            deck.append({'suit': s, 'rank': r, 'value': RANK_VALUES[r]})
    random.shuffle(deck)
    return deck

def get_min_bid(score):
    if score >= 30: return 5
    if score >= 27: return 4
    if score >= 21: return 3
    return 2

class TarneebRoom:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = []  # قائمة اللاعبين [{id, name, is_bot, sid, score, hand, bids, tricks}]
        self.game_started = False
        self.dealer_idx = 0
        self.current_turn = 0
        self.state = 'WAITING'  # WAITING, BIDDING, PLAYING, ROUND_OVER
        self.total_bids = 0
        self.bids_collected = 0
        self.current_trick = [] # [{player_idx, card}]
        self.trick_lead_suit = None
        self.round_first_card_played = False
        self.trump_suit = 'Hearts'

    def add_player(self, name, sid=None, is_bot=False):
        if len(self.players) >= 4: return False
        idx = len(self.players)
        self.players.append({
            'idx': idx,
            'name': name,
            'sid': sid,
            'is_bot': is_bot,
            'score': 0,
            'hand': [],
            'bid': 0,
            'tricks_won': 0
        })
        return True

    def fill_with_bots(self):
        bot_count = 1
        while len(self.players) < 4:
            self.add_player(f"بوت {bot_count}", is_bot=True)
            bot_count += 1

    def start_new_round(self):
        deck = create_deck()
        for p in self.players:
            p['hand'] = sorted(deck[p['idx']*13 : (p['idx']+1)*13], key=lambda x: (x['suit'], x['value']))
            p['bid'] = 0
            p['tricks_won'] = 0
        
        self.state = 'BIDDING'
        self.total_bids = 0
        self.bids_collected = 0
        self.current_turn = (self.dealer_idx + 1) % 4
        self.trick_lead_suit = None
        self.current_trick = []
        self.round_first_card_played = False

    def process_bid(self, player_idx, bid_val):
        p = self.players[player_idx]
        min_b = get_min_bid(p['score'])
        
        # التأكد من الحد الأدنى ما لم يطلب صفر
        if bid_val != 0 and bid_val < min_b and bid_val != 13:
            bid_val = min_b

        # الشرط الإجباري: الموزع يغطي العجز إذا لم يصل المجموع إلى 11
        if player_idx == self.dealer_idx:
            needed = 11 - self.total_bids
            if needed > 0 and bid_val < needed:
                bid_val = max(bid_val, needed)

        p['bid'] = bid_val
        self.total_bids += bid_val
        self.bids_collected += 1

        if self.bids_collected == 4:
            self.state = 'PLAYING'
            self.current_turn = (self.dealer_idx + 1) % 4
        else:
            self.current_turn = (self.current_turn + 1) % 4

    def validate_card_play(self, player_idx, card):
        p = self.players[player_idx]
        
        # القوانين الأساسية: عدم لعب الهاس أول ورقة بالجولة
        if not self.round_first_card_played and card['suit'] == 'Hearts':
            # إلا إذا كانت يده كلها هاس
            if not all(c['suit'] == 'Hearts' for c in p['hand']):
                return False, "ممنوع تنزل قلب أحمر (هاس) في أول أكلة بالجولة!"

        # إجبارية اتباع النوع (Suit)
        if self.trick_lead_suit:
            has_lead_suit = any(c['suit'] == self.trick_lead_suit for c in p['hand'])
            if has_lead_suit and card['suit'] != self.trick_lead_suit:
                return False, f"إجباري تلعب نفس النوع ({self.trick_lead_suit})!"

        return True, "ناجح"

    def play_card(self, player_idx, card):
        p = self.players[player_idx]
        p['hand'] = [c for c in p['hand'] if not (c['suit'] == card['suit'] and c['rank'] == card['rank'])]
        
        if len(self.current_trick) == 0:
            self.trick_lead_suit = card['suit']

        self.current_trick.append({'player_idx': player_idx, 'card': card})
        self.round_first_card_played = True

        if len(self.current_trick) == 4:
            return self.resolve_trick()
        else:
            self.current_turn = (self.current_turn + 1) % 4
            return None

    def resolve_trick(self):
        # تحديد الفائز بالرقعة
        winning_play = self.current_trick[0]
        for play in self.current_trick[1:]:
            curr_c = play['card']
            win_c = winning_play['card']
            
            if curr_c['suit'] == 'Hearts' and win_c['suit'] != 'Hearts':
                winning_play = play
            elif curr_c['suit'] == win_c['suit'] and curr_c['value'] > win_c['value']:
                winning_play = play

        winner_idx = winning_play['player_idx']
        self.players[winner_idx]['tricks_won'] += 1

        # تطبيق قانون القس وكينج الهاس (Hearts)
        has_ace_hearts = any(p['card']['suit'] == 'Hearts' and p['card']['rank'] == 'A' for p in self.current_trick)
        has_king_hearts = any(p['card']['suit'] == 'Hearts' and p['card']['rank'] == 'K' for p in self.current_trick)
        
        if has_ace_hearts and has_king_hearts:
            for p in self.current_trick:
                if p['card']['suit'] == 'Hearts' and p['card']['rank'] == 'A':
                    self.players[p['player_idx']]['score'] += 5
                if p['card']['suit'] == 'Hearts' and p['card']['rank'] == 'K':
                    self.players[p['player_idx']]['score'] -= 5

        completed_trick = list(self.current_trick)
        self.current_trick = []
        self.trick_lead_suit = None
        self.current_turn = winner_idx

        # فحص انتهاء الشوط (13 أكلة)
        if sum(p['tricks_won'] for p in self.players) == 13:
            self.calculate_round_scores()
            self.dealer_idx = (self.dealer_idx + 1) % 4
            self.state = 'ROUND_OVER'

        return {'winner': winner_idx, 'trick': completed_trick}

    def calculate_round_scores(self):
        game_over = False
        winner_name = ""
        for p in self.players:
            bid = p['bid']
            won = p['tricks_won']
            
            if bid == 0:
                p['score'] += 5 if won == 0 else -5
            elif bid == 7:
                p['score'] += 14 if won >= 7 else -14
            elif bid == 9:
                p['score'] += 21 if won >= 9 else -21
            elif bid == 13:
                p['score'] += 52 if won == 13 else -52
            else:
                p['score'] += bid if won >= bid else -bid

            if p['score'] >= 31:
                game_over = True
                winner_name = p['name']

        if game_over:
            self.state = 'GAME_OVER'

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def on_join(data):
    room_id = data.get('room', 'default')
    player_name = data.get('name', 'لاعب')
    join_room(room_id)

    if room_id not in ROOMS:
        ROOMS[room_id] = TarneebRoom(room_id)
    
    room = ROOMS[room_id]
    
    if not room.game_started:
        added = room.add_player(player_name, sid=request.sid)
        if not added:
            emit('error', {'msg': 'الغرفة مكتملة!'})
            return
        
        emit('update_room', get_room_state(room), to=room_id)

@socketio.on('start_game')
def on_start(data):
    room_id = data.get('room')
    room = ROOMS.get(room_id)
    if room and not room.game_started:
        room.fill_with_bots()
        room.game_started = True
        room.start_new_round()
        broadcast_state(room)
        check_bot_turn(room)

@socketio.on('submit_bid')
def on_bid(data):
    room = ROOMS.get(data.get('room'))
    if room and room.state == 'BIDDING':
        p_idx = get_player_idx_by_sid(room, request.sid)
        if p_idx == room.current_turn:
            room.process_bid(p_idx, int(data.get('bid', 2)))
            broadcast_state(room)
            check_bot_turn(room)

@socketio.on('play_card')
def on_play_card(data):
    room = ROOMS.get(data.get('room'))
    if room and room.state == 'PLAYING':
        p_idx = get_player_idx_by_sid(room, request.sid)
        if p_idx == room.current_turn:
            card = data.get('card')
            valid, msg = room.validate_card_play(p_idx, card)
            if not valid:
                emit('error', {'msg': msg})
                return
            
            res = room.play_card(p_idx, card)
            broadcast_state(room)
            if res:
                socketio.sleep(1.5)
                broadcast_state(room)
            check_bot_turn(room)

def check_bot_turn(room):
    while room.game_started and room.players[room.current_turn]['is_bot'] and room.state in ['BIDDING', 'PLAYING']:
        socketio.sleep(1)
        curr_bot = room.players[room.current_turn]
        bot_idx = room.current_turn
        
        if room.state == 'BIDDING':
            min_b = get_min_bid(curr_bot['score'])
            room.process_bid(bot_idx, min_b)
            broadcast_state(room)
        
        elif room.state == 'PLAYING':
            # ذكاء اصطناعي مبسط للبوت للعب كارت قانوني
            valid_cards = []
            for c in curr_bot['hand']:
                ok, _ = room.validate_card_play(bot_idx, c)
                if ok: valid_cards.append(c)
            
            if valid_cards:
                chosen_card = random.choice(valid_cards)
                res = room.play_card(bot_idx, chosen_card)
                broadcast_state(room)
                if res:
                    socketio.sleep(1.5)
                    broadcast_state(room)

def get_player_idx_by_sid(room, sid):
    for p in room.players:
        if p['sid'] == sid: return p['idx']
    return -1

def get_room_state(room):
    return {
        'room_id': room.room_id,
        'game_started': room.game_started,
        'state': room.state,
        'current_turn': room.current_turn,
        'dealer_idx': room.dealer_idx,
        'total_bids': room.total_bids,
        'current_trick': room.current_trick,
        'players': [{'name': p['name'], 'score': p['score'], 'bid': p['bid'], 'tricks_won': p['tricks_won'], 'is_bot': p['is_bot'], 'cards_count': len(p['hand'])} for p in room.players]
    }

def broadcast_state(room):
    for p in room.players:
        if not p['is_bot'] and p['sid']:
            p_state = get_room_state(room)
            p_state['my_hand'] = p['hand']
            p_state['my_index'] = p['idx']
            p_state['min_bid'] = get_min_bid(p['score'])
            socketio.emit('game_update', p_state, to=p['sid'])

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000)
