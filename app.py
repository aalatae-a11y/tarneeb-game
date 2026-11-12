import os
import random
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'tarneeb_libya_secret_key_2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_timeout=60, ping_interval=25)

ROOMS = {}

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
        self.waiting_timer_running = False
        self.wait_seconds_left = 30
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
        self.trick_winner_idx = None

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
            'king_ace_delta': 0
        })
        return idx

    def fill_with_bots(self):
        bot_count = 1
        while len(self.players) < 4:
            self.add_player(f"بوت {bot_count}", is_bot=True)
            bot_count += 1

    def check_redeal_eligibility(self, player_idx):
        if self.round_number <= 1 or self.state != 'BIDDING' or self.bids_collected > 0:
            return False
        hand = self.players[player_idx]['hand']
        if len(hand) != 13:
            return False
        
        suit_counts = {s: 0 for s in SUITS}
        for c in hand:
            suit_counts[c['suit']] += 1
        if any(count >= 7 for count in suit_counts.values()):
            return True
            
        hearts = [c for c in hand if c['suit'] == 'Hearts']
        if len(hearts) == 1 and hearts[0]['rank'] == 'K':
            return True
            
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
        self.trick_winner_idx = None

    def get_dealer_min_bid(self, player_idx):
        min_b = get_min_bid(self.players[player_idx]['score'])
        if player_idx == self.dealer_idx:
            needed = 11 - self.total_bids
            if needed > min_b:
                return needed
        return min_b

    def process_bid(self, player_idx, bid_val):
        p = self.players[player_idx]
        min_b = get_min_bid(p['score'])
        
        if player_idx == self.dealer_idx:
            needed = 11 - self.total_bids
            if needed > 0:
                if bid_val < needed:
                    bid_val = needed
            else:
                if bid_val != 0 and bid_val < min_b and bid_val != 13:
                    bid_val = min_b
        else:
            if bid_val != 0 and bid_val < min_b and bid_val != 13:
                bid_val = min_b

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
        
        if not self.round_first_card_played and card['suit'] == 'Hearts':
            if not all(c['suit'] == 'Hearts' for c in p['hand']):
                return False, "ممنوع تنزل قلب أحمر كأول ورقة بعد التوزيع!"

        if self.trick_lead_suit:
            has_lead_suit = any(c['suit'] == self.trick_lead_suit for c in p['hand'])
            if has_lead_suit and card['suit'] != self.trick_lead_suit:
                return False, f"إجباري تلعب نفس النوع ({self.trick_lead_suit})!"

        return True, "ناجح"

    def play_card_step1(self, player_idx, card):
        """ينزل الورقة فقط. إذا كانت الرابعة، يبقيها ظاهرة ويدخل في حالة TRICK_PAUSE"""
        p = self.players[player_idx]
        p['hand'] = [c for c in p['hand'] if not (c['suit'] == card['suit'] and c['rank'] == card['rank'])]
        
        if len(self.current_trick) == 0:
            self.trick_lead_suit = card['suit']

        self.current_trick.append({'player_idx': player_idx, 'card': card})
        self.round_first_card_played = True

        if len(self.current_trick) == 4:
            # حساب الفائز بالأكلة لتمريره للواجهة من أجل الأنيميشن
            winning_play = self.current_trick[0]
            for play in self.current_trick[1:]:
                curr_c = play['card']
                win_c = winning_play['card']
                if curr_c['suit'] == 'Hearts' and win_c['suit'] != 'Hearts':
                    winning_play = play
                elif curr_c['suit'] == win_c['suit'] and curr_c['value'] > win_c['value']:
                    winning_play = play
            
            self.trick_winner_idx = winning_play['player_idx']
            self.state = 'TRICK_PAUSE'
            return True # الأكلة اكتملت وتحتاج وقفة ثانيتين
        else:
            self.current_turn = (self.current_turn + 1) % 4
            return False

    def finalize_trick_step2(self):
        """يُستدعى بعد مرور الثانيتين لمسح الأكلة واحتساب النقاط"""
        if self.state != 'TRICK_PAUSE' or len(self.current_trick) != 4:
            return

        # قانون الكينج والآس (K♥ و A♥): مفعل ابتداءً من الدورة الثانية
        if self.round_number > 1:
            king_owner = None
            ace_owner = None
            for play in self.current_trick:
                if play['card']['suit'] == 'Hearts':
                    if play['card']['rank'] == 'K':
                        king_owner = play['player_idx']
                    elif play['card']['rank'] == 'A':
                        ace_owner = play['player_idx']

            if king_owner is not None and ace_owner is not None:
                self.players[king_owner]['score'] -= 5
                self.players[king_owner]['king_ace_delta'] -= 5
                self.players[ace_owner]['score'] += 5
                self.players[ace_owner]['king_ace_delta'] += 5

        winner_idx = self.trick_winner_idx
        self.players[winner_idx]['tricks_won'] += 1
        self.players[winner_idx]['won_cards_count'] += 4

        self.last_completed_trick = list(self.current_trick)
        self.current_trick = []
        self.trick_lead_suit = None
        self.trick_winner_idx = None
        self.current_turn = winner_idx

        if sum(p['tricks_won'] for p in self.players) == 13:
            game_ended = self.calculate_round_scores()
            if game_ended:
                self.state = 'GAME_OVER'
            else:
                self.state = 'ROUND_OVER'
                self.dealer_idx = (self.dealer_idx + 1) % 4
        else:
            self.state = 'PLAYING'

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
                if won == 0:
                    round_pts = 5
                    reason = "نجح في الصفر (+5)"
                else:
                    round_pts = -5
                    reason = f"خسر الصفر بأكل {won} (-5)"
            elif bid == 7:
                if won >= 7:
                    round_pts = 14
                    reason = "حقق طلب 7 (+14)"
                else:
                    round_pts = -14
                    reason = "كسر طلب 7 (-14)"
            elif bid == 9:
                if won >= 9:
                    round_pts = 27
                    reason = "حقق طلب 9 (+27)"
                else:
                    round_pts = -27
                    reason = "كسر طلب 9 (-27)"
            elif bid == 13:
                if won == 13:
                    round_pts = 31
                    reason = "حقق الكبسة 13 (فوز فوري!)"
                    instant_winner = p['name']
                else:
                    round_pts = -31
                    reason = "خسر الكبسة 13 (خسارة فورية!)"
                    instant_loser = p['name']
            else:
                if won >= bid:
                    round_pts = bid
                    reason = f"حقق طلبه {bid} (+{bid})"
                else:
                    round_pts = -bid
                    reason = f"كسر طلبه {bid} (-{bid})"

            p['score'] += round_pts
            total_round_change = round_pts + ka_delta
            
            ka_note = ""
            if ka_delta > 0: ka_note = " | أكل K♥ بـ A♥ (+5)"
            elif ka_delta < 0: ka_note = " | انأكل K♥ بـ A♥ (-5)"

            self.round_summary.append({
                'name': p['name'],
                'bid': bid,
                'won': won,
                'change': total_round_change,
                'total': p['score'],
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
    join_room(room_id)

    if room_id not in ROOMS:
        ROOMS[room_id] = TarneebRoom(room_id)
    
    room = ROOMS[room_id]
    idx = room.add_player(player_name, sid=request.sid)
    if idx is None:
        emit('error', {'msg': 'الغرفة مكتملة!'})
        return
    
    # إذا اكتمل 4 لاعبين حقيقيين، ابدأ فوراً بدون انتظار
    if len(room.players) == 4 and not room.game_started:
        room.game_started = True
        room.waiting_timer_running = False
        room.start_new_round(increment_round=True)
        broadcast_state(room)
        socketio.start_background_task(run_bots_async, room)
        return

    broadcast_state(room)

    # تشغيل مؤقت الانتظار 30 ثانية مرة واحدة للغرفة
    if not room.game_started and not room.waiting_timer_running:
        room.waiting_timer_running = True
        room.wait_seconds_left = 30
        socketio.start_background_task(waiting_countdown_task, room)

def waiting_countdown_task(room):
    while room.wait_seconds_left > 0 and not room.game_started and len(room.players) < 4:
        broadcast_state(room)
        socketio.sleep(1)
        room.wait_seconds_left -= 1

    if not room.game_started:
        room.fill_with_bots()
        room.game_started = True
        room.waiting_timer_running = False
        room.start_new_round(increment_round=True)
        broadcast_state(room)
        socketio.start_background_task(run_bots_async, room)

@socketio.on('force_start_bots')
def on_force_start(data):
    room = ROOMS.get(data.get('room'))
    if room and not room.game_started:
        room.fill_with_bots()
        room.game_started = True
        room.waiting_timer_running = False
        room.start_new_round(increment_round=True)
        broadcast_state(room)
        socketio.start_background_task(run_bots_async, room)

@socketio.on('request_redeal')
def on_redeal(data):
    room = ROOMS.get(data.get('room'))
    if room:
        p_idx = get_player_idx_by_sid(room, request.sid)
        if p_idx != -1 and room.check_redeal_eligibility(p_idx):
            room.start_new_round(increment_round=False)
            broadcast_state(room)
            socketio.start_background_task(run_bots_async, room)

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
        socketio.start_background_task(run_bots_async, room)

@socketio.on('submit_bid')
def on_bid(data):
    room = ROOMS.get(data.get('room'))
    if room and room.state == 'BIDDING':
        p_idx = get_player_idx_by_sid(room, request.sid)
        if p_idx == room.current_turn:
            room.process_bid(p_idx, int(data.get('bid', 2)))
            broadcast_state(room)
            socketio.start_background_task(run_bots_async, room)

@socketio.on('play_card')
def on_play_card(data):
    room = ROOMS.get(data.get('room'))
    if not room:
        emit('error', {'msg': 'انتهت جلسة السيرفر، يرجى تحديث الصفحة'})
        return
    if room.state == 'PLAYING':
        p_idx = get_player_idx_by_sid(room, request.sid)
        if p_idx == room.current_turn:
            card = data.get('card')
            valid, msg = room.validate_card_play(p_idx, card)
            if not valid:
                emit('error', {'msg': msg})
                return
            
            trick_done = room.play_card_step1(p_idx, card)
            broadcast_state(room)
            
            if trick_done:
                socketio.start_background_task(handle_trick_pause_and_continue, room)
            else:
                socketio.start_background_task(run_bots_async, room)

def handle_trick_pause_and_continue(room):
    # السيرفر يبقي الكروت الأربعة ظاهرة للجميع لمدة 2.3 ثانية (ثانيتين للرؤية + 0.3 للانيميشن)
    socketio.sleep(2.3)
    room.finalize_trick_step2()
    broadcast_state(room)
    if room.state == 'PLAYING':
        socketio.start_background_task(run_bots_async, room)

def run_bots_async(room):
    socketio.sleep(0.4)
    while room.game_started and room.state in ['BIDDING', 'PLAYING']:
        p_idx = room.current_turn
        p = room.players[p_idx]
        if not p['is_bot']:
            break
        
        socketio.sleep(0.6)
        if room.state == 'BIDDING':
            min_b = room.get_dealer_min_bid(p_idx)
            room.process_bid(p_idx, min_b)
            broadcast_state(room)
        elif room.state == 'PLAYING':
            valid_cards = [c for c in p['hand'] if room.validate_card_play(p_idx, c)[0]]
            if valid_cards:
                chosen = random.choice(valid_cards)
                trick_done = room.play_card_step1(p_idx, chosen)
                broadcast_state(room)
                if trick_done:
                    # البوت يتوقف فوراً لأن الأكلة اكتملت، وتستلم مهمة التوقيت الدفة
                    handle_trick_pause_and_continue(room)
                    break
        else:
            break

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
                'wait_seconds_left': room.wait_seconds_left,
                'state': room.state,
                'round_number': room.round_number,
                'current_turn': room.current_turn,
                'dealer_idx': room.dealer_idx,
                'total_bids': room.total_bids,
                'current_trick': room.current_trick,
                'trick_winner_idx': room.trick_winner_idx,
                'last_trick': room.last_completed_trick,
                'winner_message': room.winner_message,
                'round_summary': room.round_summary,
                'my_index': p_idx,
                'my_hand': p['hand'],
                'min_bid': min_b,
                'is_dealer': is_dealer,
                'can_redeal': can_redeal,
                'players': [{
                    'name': x['name'], 
                    'score': x['score'], 
                    'bid': x['bid'], 
                    'tricks_won': x['tricks_won'], 
                    'won_cards_count': x['won_cards_count'],
                    'is_bot': x['is_bot'], 
                    'cards_count': len(x['hand'])
                } for x in room.players]
            }
            socketio.emit('game_update', p_state, to=p['sid'])

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
