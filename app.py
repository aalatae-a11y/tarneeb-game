import os
import random
import time
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'tarneeb_libya_secret_key_2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_timeout=60, ping_interval=25)

ROOMS = {}
BANNED_PLAYERS = {}  # حفظ اللاعبين المطرودين لكل غرفة {room_id: set(names)}

SUITS = ['Hearts', 'Tiles', 'Clovers', 'Pikes']
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
        self.players = []
        self.game_started = False
        self.waiting_for_players = True
        self.waiting_timer_started = False
        self.dealer_idx = 0
        self.current_turn = 0
        self.state = 'WAITING'
        self.round_number = 0
        self.total_bids = 0
        self.bids_collected = 0
        self.current_trick = []
        self.last_completed_trick = []
        self.trick_lead_suit = None
        self.round_first_card_played = False
        self.winner_message = ""
        self.round_summary = []
        self.turn_timestamp = 0

    def add_player(self, name, sid=None, is_bot=False):
        for p in self.players:
            if p['name'] == name and not p['is_bot']:
                p['sid'] = sid
                return p['idx']

        if len(self.players) >= 4: return None
        idx = len(self.players)
        self.players.append({
            'idx': idx,
            'name': name,
            'sid': sid,
            'is_bot': is_bot,
            'score': 0,
            'hand': [],
            'bid': 0,
            'tricks_won': 0,
            'won_cards_count': 0,
            'king_ace_delta': 0,
            'timeout_streak': 0  # عداد انتهاء الوقت للتتالي
        })
        return idx

    def fill_with_bots(self):
        bot_count = 1
        while len(self.players) < 4:
            self.add_player(f"بوت {bot_count}", is_bot=True)
            bot_count += 1
        self.waiting_for_players = False

    def replace_player_with_bot(self, player_idx):
        p = self.players[player_idx]
        p['name'] = f"بوت منسحب {player_idx}"
        p['is_bot'] = True
        p['sid'] = None
        p['timeout_streak'] = 0

    def check_redeal_eligibility(self, player_idx):
        if self.round_number <= 1 or self.state != 'BIDDING' or self.bids_collected > 0:
            return False
        hand = self.players[player_idx]['hand']
        if len(hand) != 13: return False
        
        suit_counts = {s: 0 for s in SUITS}
        for c in hand: suit_counts[c['suit']] += 1
        if any(count >= 7 for count in suit_counts.values()): return True
            
        hearts = [c for c in hand if c['suit'] == 'Hearts']
        if len(hearts) == 1 and hearts[0]['rank'] == 'K': return True
            
        return False

    def start_new_round(self, increment_round=True):
        if increment_round:
            self.round_number += 1
        deck = create_deck()
        for p in self.players:
            p['hand'] = sorted(deck[p['idx']*13 : (p['idx']+1)*13], key=lambda x: (x['suit'], x['value']))
            p['bid'] = 0
            p['tricks_won'] = 0
            p['won_cards_count'] = 0
            p['king_ace_delta'] = 0
        
        self.state = 'BIDDING'
        self.total_bids = 0
        self.bids_collected = 0
        self.current_turn = (self.dealer_idx + 1) % 4
        self.trick_lead_suit = None
        self.current_trick = []
        self.last_completed_trick = []
        self.round_first_card_played = False
        self.round_summary = []
        self.turn_timestamp = time.time()

    def get_dealer_min_bid(self, player_idx):
        min_b = get_min_bid(self.players[player_idx]['score'])
        if player_idx == self.dealer_idx:
            needed = 11 - self.total_bids
            if needed > min_b: return needed
        return min_b

    def process_bid(self, player_idx, bid_val):
        p = self.players[player_idx]
        min_b = get_min_bid(p['score'])
        
        if player_idx == self.dealer_idx:
            needed = 11 - self.total_bids
            if needed > 0:
                if bid_val < needed: bid_val = needed
            else:
                if bid_val != 0 and bid_val < min_b and bid_val != 13: bid_val = min_b
        else:
            if bid_val != 0 and bid_val < min_b and bid_val != 13: bid_val = min_b

        p['bid'] = bid_val
        self.total_bids += bid_val
        self.bids_collected += 1

        if self.bids_collected == 4:
            self.state = 'PLAYING'
            self.current_turn = (self.dealer_idx + 1) % 4
        else:
            self.current_turn = (self.current_turn + 1) % 4
        self.turn_timestamp = time.time()

    def validate_card_play(self, player_idx, card):
        p = self.players[player_idx]
        if not self.round_first_card_played and card['suit'] == 'Hearts':
            if not all(c['suit'] == 'Hearts' for c in p['hand']):
                return False, "ممنوع تنزل قلب أحمر كأول ورقة بعد التوزيع!"

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
            self.turn_timestamp = time.time()
            return None

    def resolve_trick(self):
        if self.round_number > 1:
            king_owner = None
            ace_owner = None
            for play in self.current_trick:
                if play['card']['suit'] == 'Hearts':
                    if play['card']['rank'] == 'K': king_owner = play['player_idx']
                    elif play['card']['rank'] == 'A': ace_owner = play['player_idx']

            if king_owner is not None and ace_owner is not None:
                self.players[king_owner]['score'] -= 5
                self.players[king_owner]['king_ace_delta'] -= 5
                self.players[ace_owner]['score'] += 5
                self.players[ace_owner]['king_ace_delta'] += 5

        winning_play = self.current_trick[0]
        for play in self.current_trick[1:]:
            curr_c = play['card']
            win_c = winning_play['card']
            if curr_c['suit'] == 'Hearts' and win_c['suit'] != 'Hearts': winning_play = play
            elif curr_c['suit'] == win_c['suit'] and curr_c['value'] > win_c['value']: winning_play = play

        winner_idx = winning_play['player_idx']
        self.players[winner_idx]['tricks_won'] += 1
        self.players[winner_idx]['won_cards_count'] += 4

        self.last_completed_trick = list(self.current_trick)
        completed_trick = list(self.current_trick)
        
        self.current_trick = []
        self.trick_lead_suit = None
        self.current_turn = winner_idx
        self.turn_timestamp = time.time()

        if sum(p['tricks_won'] for p in self.players) == 13:
            game_ended = self.calculate_round_scores()
            if game_ended: self.state = 'GAME_OVER'
            else:
                self.state = 'ROUND_OVER'
                self.dealer_idx = (self.dealer_idx + 1) % 4

        return {'winner': winner_idx, 'trick': completed_trick}

    def calculate_round_scores(self):
        self.round_summary = []
        instant_winner = None
        instant_loser = None

        for p in self.players:
            bid = p['bid']
            won = p['tricks_won']
            ka_delta = p['king_ace_delta']
            round_pts = 0
            reason = ""

            if bid == 0:
                if won == 0: round_pts = 5; reason = "نجح في الصفر (+5)"
                else: round_pts = -5; reason = f"خسر الصفر بأكل {won} (-5)"
            elif bid == 7:
                if won >= 7: round_pts = 14; reason = f"حقق طلب 7 (+14)"
                else: round_pts = -14; reason = f"كسر طلب 7 (-14)"
            elif bid == 9:
                if won >= 9: round_pts = 27; reason = f"حقق طلب 9 (+27)"
                else: round_pts = -27; reason = f"كسر طلب 9 (-27)"
            elif bid == 13:
                if won == 13: round_pts = 31; reason = "حقق الكبسة 13 (فوز فوري!)"; instant_winner = p['name']
                else: round_pts = -31; reason = "خسر الكبسة 13 (خسارة فورية!)"; instant_loser = p['name']
            else:
                if won >= bid: round_pts = bid; reason = f"حقق طلبه {bid} (+{bid})"
                else: round_pts = -bid; reason = f"كسر طلبه {bid} (-{bid})"

            p['score'] += round_pts
            total_round_change = round_pts + ka_delta
            
            ka_note = ""
            if ka_delta > 0: ka_note = " | أكل K♥ بـ A♥ (+5)"
            elif ka_delta < 0: ka_note = " | انأكل K♥ بـ A♥ (-5)"

            self.round_summary.append({
                'name': p['name'], 'bid': bid, 'won': won,
                'change': total_round_change, 'total': p['score'],
                'detail': reason + ka_note
            })

        if instant_winner:
            self.winner_message = f"🏆 مبروك للبطل {instant_winner} الفوز بالكبسة (13)!"
            return True

        max_score = max(p['score'] for p in self.players)
        min_score = min(p['score'] for p in self.players)
        
        if max_score >= 31:
            winners = [p['name'] for p in self.players if p['score'] == max_score]
            self.winner_message = f"🏆 مبروك للبطل: {' و '.join(winners)} لتجاوز 31 نقطة!"
            return True
        if min_score <= -31 or instant_loser:
            leader = [p['name'] for p in self.players if p['score'] == max_score]
            self.winner_message = f"🏆 انتهت اللعبة! الفائز الأعلى نقاطاً: {' و '.join(leader)}"
            return True
            
        return False

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def on_join(data):
    room_id = data.get('room', 'tripoli')
    player_name = data.get('name', 'عبد الله')

    # التحقق مما إذا كان اللاعب مطروداً من هذه الغرفة مسبقاً
    if room_id in BANNED_PLAYERS and player_name in BANNED_PLAYERS[room_id]:
        emit('error', {'msg': '🚫 تم طردك من هذه الغرفة بسبب تجاوز وقت التفكير!'})
        return

    join_room(room_id)

    if room_id not in ROOMS:
        ROOMS[room_id] = TarneebRoom(room_id)
    
    room = ROOMS[room_id]
    idx = room.add_player(player_name, sid=request.sid)
    if idx is None:
        emit('error', {'msg': 'الغرفة مكتملة!'})
        return
    
    if room.waiting_for_players and not room.waiting_timer_started:
        room.waiting_timer_started = True
        socketio.start_background_task(wait_for_players_task, room)

    broadcast_state(room)

def wait_for_players_task(room):
    start_t = time.time()
    while room.waiting_for_players and time.time() - start_t < 30:
        socketio.sleep(1)
        if len(room.players) == 4:
            break
    
    if room.waiting_for_players and not room.game_started:
        room.fill_with_bots()
        room.game_started = True
        room.start_new_round(increment_round=True)
        broadcast_state(room)
        socketio.start_background_task(run_game_loop, room)

@socketio.on('start_game')
def on_start(data):
    room = ROOMS.get(data.get('room'))
    if room and not room.game_started:
        room.waiting_for_players = False
        room.fill_with_bots()
        room.game_started = True
        room.start_new_round(increment_round=True)
        broadcast_state(room)
        socketio.start_background_task(run_game_loop, room)

@socketio.on('request_redeal')
def on_redeal(data):
    room = ROOMS.get(data.get('room'))
    if room:
        p_idx = get_player_idx_by_sid(room, request.sid)
        if p_idx != -1 and room.check_redeal_eligibility(p_idx):
            room.start_new_round(increment_round=False)
            broadcast_state(room)

@socketio.on('next_round')
def on_next_round(data):
    room = ROOMS.get(data.get('room'))
    if room and room.state in ['ROUND_OVER', 'GAME_OVER']:
        if room.state == 'GAME_OVER':
            for p in room.players: p['score'] = 0
            room.dealer_idx = 0
            room.round_number = 0
        room.start_new_round(increment_round=True)
        broadcast_state(room)

@socketio.on('submit_bid')
def on_bid(data):
    room = ROOMS.get(data.get('room'))
    if room and room.state == 'BIDDING':
        p_idx = get_player_idx_by_sid(room, request.sid)
        if p_idx == room.current_turn:
            room.players[p_idx]['timeout_streak'] = 0  # تصفير العداد عند الاستجابة في الوقت
            room.process_bid(p_idx, int(data.get('bid', 2)))
            broadcast_state(room)

@socketio.on('play_card')
def on_play_card(data):
    room = ROOMS.get(data.get('room'))
    if not room: return
    if room.state == 'PLAYING':
        p_idx = get_player_idx_by_sid(room, request.sid)
        if p_idx == room.current_turn:
            card = data.get('card')
            valid, msg = room.validate_card_play(p_idx, card)
            if not valid:
                emit('error', {'msg': msg})
                return
            room.players[p_idx]['timeout_streak'] = 0  # تصفير العداد عند اللعب في الوقت
            room.play_card(p_idx, card)
            broadcast_state(room)

def run_game_loop(room):
    while room.game_started and room.state in ['BIDDING', 'PLAYING']:
        socketio.sleep(0.3)
        p_idx = room.current_turn
        p = room.players[p_idx]

        # البوتات لا يتطبق عليها وقت التفكير نهائياً
        if p['is_bot']:
            socketio.sleep(0.5)
            if room.state == 'BIDDING':
                min_b = room.get_dealer_min_bid(p_idx)
                room.process_bid(p_idx, min_b)
                broadcast_state(room)
            elif room.state == 'PLAYING':
                valid_cards = [c for c in p['hand'] if room.validate_card_play(p_idx, c)[0]]
                if valid_cards:
                    chosen = random.choice(valid_cards)
                    room.play_card(p_idx, chosen)
                    broadcast_state(room)
            continue

        # اللاعب الحقيقي: مؤقت التفكير 20 ثانية
        if time.time() - room.turn_timestamp > 20:
            p['timeout_streak'] += 1  # زيادة عداد انتهاء الوقت المتتالي
            
            # إذا وصل التتالي إلى 3 مرات -> طرد من الغرفة وحظر العودة
            if p['timeout_streak'] >= 3:
                p_name = p['name']
                if room.room_id not in BANNED_PLAYERS:
                    BANNED_PLAYERS[room.room_id] = set()
                BANNED_PLAYERS[room.room_id].add(p_name)
                
                if p['sid']:
                    socketio.emit('error', {'msg': '🚫 تم طردك من الغرفة لتجاوز الوقت 3 مرات متتالية!'}, to=p['sid'])
                
                # استبدال اللاعب ببوْت لكي لا تتعطل اللعبة
                room.replace_player_with_bot(p_idx)
                broadcast_state(room)
                continue

            # انتهاء وقت عادي (أقل من 3 مرات): تنفيذ إجراء تلقائي
            if room.state == 'BIDDING':
                min_b = room.get_dealer_min_bid(p_idx)
                room.process_bid(p_idx, min_b)
                broadcast_state(room)
            elif room.state == 'PLAYING':
                valid_cards = [c for c in p['hand'] if room.validate_card_play(p_idx, c)[0]]
                if valid_cards:
                    room.play_card(p_idx, valid_cards[0])
                    broadcast_state(room)

def get_player_idx_by_sid(room, sid):
    for p in room.players:
        if p['sid'] == sid: return p['idx']
    return -1

def broadcast_state(room):
    for p in room.players:
        if not p['is_bot'] and p['sid']:
            p_idx = p['idx']
            can_redeal = room.check_redeal_eligibility(p_idx)
            min_b = room.get_dealer_min_bid(p_idx)
            is_dealer = (p_idx == room.dealer_idx)

            p_state = {
                'room_id': room.room_id,
                'game_started': room.game_started,
                'waiting_for_players': room.waiting_for_players,
                'state': room.state,
                'round_number': room.round_number,
                'current_turn': room.current_turn,
                'dealer_idx': room.dealer_idx,
                'total_bids': room.total_bids,
                'current_trick': room.current_trick,
                'last_trick': room.last_completed_trick,
                'winner_message': room.winner_message,
                'round_summary': room.round_summary,
                'my_index': p_idx,
                'my_hand': p['hand'],
                'min_bid': min_b,
                'is_dealer': is_dealer,
                'can_redeal': can_redeal,
                'players': [{
                    'name': x['name'], 'score': x['score'], 'bid': x['bid'],
                    'tricks_won': x['tricks_won'], 'won_cards_count': x['won_cards_count'],
                    'is_bot': x['is_bot'], 'cards_count': len(x['hand'])
                } for x in room.players]
            }
            socketio.emit('game_update', p_state, to=p['sid'])

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
                
