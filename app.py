import os
import random
import time
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'tarneeb_libya_secret_key_2026'
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=60,
    ping_interval=25
)

ROOMS = {}

SUITS = ['Hearts', 'Tiles', 'Clovers', 'Pikes']
HAND_SUIT_ORDER = {'Tiles': 0, 'Clovers': 1, 'Hearts': 2, 'Pikes': 3}
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
RANK_VALUES = {r: i for i, r in enumerate(RANKS, start=2)}

ALLOWED_BIDS = [2, 3, 4, 5, 7, 9, 13]
BID_TIMEOUT_SECONDS = 20
TURN_TIMEOUT_SECONDS = 30


def create_deck():
    deck = []
    for s in SUITS:
        for r in RANKS:
            deck.append({'suit': s, 'rank': r, 'value': RANK_VALUES[r]})
    random.shuffle(deck)
    return deck


def get_min_bid(score):
    if score >= 30:
        return 5
    if score >= 27:
        return 4
    if score >= 21:
        return 3
    return 2


def snap_to_allowed_bid(val):
    for b in ALLOWED_BIDS:
        if b >= val:
            return b
    return 13


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
        self.is_processing_trick = False
        self.turn_token = 0
        self.action_deadline = 0.0
        self.action_seconds_left = 0

    def bump_turn_token(self):
        self.turn_token += 1
        return self.turn_token

    def set_action_timer(self, seconds):
        self.action_deadline = time.time() + seconds
        self.action_seconds_left = seconds

    def clear_action_timer(self):
        self.action_deadline = 0.0
        self.action_seconds_left = 0

    def add_player(self, name, sid=None, is_bot=False):
        for p in self.players:
            if p['name'] == name and not p['is_bot']:
                p['sid'] = sid
                return p['idx']

        if len(self.players) >= 4:
            return None
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
        if player_idx < 0 or player_idx >= len(self.players):
            return False
        if self.round_number == 1:
            if self.round_first_card_played:
                return False
        else:
            if self.state != 'BIDDING' or self.bids_collected > 0:
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
        self.bump_turn_token()
        self.clear_action_timer()

        deck = create_deck()
        for p in self.players:
            raw_hand = deck[p['idx'] * 13 : (p['idx'] + 1) * 13]
            p['hand'] = sorted(
                raw_hand,
                key=lambda x: (HAND_SUIT_ORDER.get(x['suit'], 99), x['value'])
            )
            p['bid'] = 0
            p['tricks_won'] = 0
            p['won_cards_count'] = 0
            p['king_ace_delta'] = 0

        if self.round_number == 1:
            self.state = 'PLAYING'
            self.total_bids = 0
            self.bids_collected = 4
        else:
            self.state = 'BIDDING'
            self.total_bids = 0
            self.bids_collected = 0

        self.current_turn = (self.dealer_idx - 1) % 4
        self.trick_lead_suit = None
        self.current_trick = []
        self.last_completed_trick = []
        self.round_first_card_played = False
        self.round_summary = []
        self.trick_winner_idx = None
        self.is_processing_trick = False

    def get_dealer_min_bid(self, player_idx):
        min_b = snap_to_allowed_bid(get_min_bid(self.players[player_idx]['score']))
        if player_idx == self.dealer_idx:
            needed = 11 - self.total_bids
            if needed > min_b:
                return snap_to_allowed_bid(needed)
        return min_b

    def get_auto_timeout_bid(self, player_idx):
        p = self.players[player_idx]
        min_b = snap_to_allowed_bid(get_min_bid(p['score']))
        auto_b = max(2, min_b)
        if player_idx == self.dealer_idx:
            needed = 11 - self.total_bids
            if needed > auto_b:
                auto_b = snap_to_allowed_bid(needed)
        return min(auto_b, 13)

    def calculate_smart_bid(self, player_idx):
        p = self.players[player_idx]
        hand = p['hand']
        min_b = snap_to_allowed_bid(get_min_bid(p['score']))

        strength = 0.0
        for c in hand:
            if c['rank'] == 'A':
                strength += 2.2
            elif c['rank'] == 'K':
                strength += 1.5
            elif c['rank'] == 'Q':
                strength += 1.0
            elif c['value'] >= 12:
                strength += 0.5

        raw_bid = int(round(strength / 2.2))
        smart_bid = snap_to_allowed_bid(max(min_b, min(raw_bid, 13)))

        if player_idx == self.dealer_idx:
            needed = 11 - self.total_bids
            if needed > 0 and smart_bid < needed:
                smart_bid = snap_to_allowed_bid(needed)
        return smart_bid

    def process_bid(self, player_idx, bid_val):
        p = self.players[player_idx]
        min_b = snap_to_allowed_bid(get_min_bid(p['score']))
        needed = 11 - self.total_bids if player_idx == self.dealer_idx else 0

        if bid_val != 0 and bid_val not in ALLOWED_BIDS:
            bid_val = snap_to_allowed_bid(bid_val)

        if player_idx == self.dealer_idx and needed > 0:
            if bid_val < needed:
                bid_val = snap_to_allowed_bid(needed)
        else:
            if bid_val != 0 and bid_val < min_b:
                bid_val = min_b

        p['bid'] = bid_val
        self.total_bids += bid_val
        self.bids_collected += 1
        self.bump_turn_token()
        self.clear_action_timer()

        if self.bids_collected == 4:
            self.state = 'PLAYING'
            self.current_turn = (self.dealer_idx - 1) % 4
        else:
            self.current_turn = (self.current_turn - 1) % 4

    def validate_card_play(self, player_idx, card):
        p = self.players[player_idx]
        if not card:
            return False, "ورقة غير صالحة!"

        if not self.round_first_card_played and card['suit'] == 'Hearts':
            if not all(c['suit'] == 'Hearts' for c in p['hand']):
                return False, "ممنوع تنزل قلب أحمر كأول ورقة بعد التوزيع!"

        if self.trick_lead_suit:
            has_lead_suit = any(c['suit'] == self.trick_lead_suit for c in p['hand'])
            if has_lead_suit and card['suit'] != self.trick_lead_suit:
                return False, f"إجباري تلعب نفس النوع ({self.trick_lead_suit})!"

        return True, "ناجح"

    def select_smart_card(self, player_idx, valid_cards):
        p = self.players[player_idx]

        if len(self.current_trick) == 0:
            aces = [c for c in valid_cards if c['rank'] == 'A']
            if aces and random.random() < 0.7:
                return random.choice(aces)
            non_hearts = [c for c in valid_cards if c['suit'] != 'Hearts']
            if non_hearts:
                return min(non_hearts, key=lambda x: x['value'])
            return min(valid_cards, key=lambda x: x['value'])

        lead_suit = self.trick_lead_suit
        current_winner_play = self.current_trick[0]
        for play in self.current_trick[1:]:
            curr_c = play['card']
            win_c = current_winner_play['card']
            if curr_c['suit'] == 'Hearts' and win_c['suit'] != 'Hearts':
                current_winner_play = play
            elif curr_c['suit'] == win_c['suit'] and curr_c['value'] > win_c['value']:
                current_winner_play = play

        if lead_suit == 'Hearts' or any(c['card']['suit'] == 'Hearts' and c['card']['rank'] == 'K' for c in self.current_trick):
            bot_aces_hearts = [c for c in valid_cards if c['suit'] == 'Hearts' and c['rank'] == 'A']
            if bot_aces_hearts:
                return bot_aces_hearts[0]

        winning_cards = [
            c for c in valid_cards
            if c['suit'] == current_winner_play['card']['suit'] and c['value'] > current_winner_play['card']['value']
        ]
        if winning_cards and p['tricks_won'] < p['bid']:
            return min(winning_cards, key=lambda x: x['value'])

        return min(valid_cards, key=lambda x: x['value'])

    def play_card(self, player_idx, card):
        if self.is_processing_trick:
            return False

        p = self.players[player_idx]
        card_matches = [c for c in p['hand'] if c['suit'] == card['suit'] and c['rank'] == card['rank']]
        if not card_matches:
            return False

        p['hand'].remove(card_matches[0])

        if len(self.current_trick) == 0:
            self.trick_lead_suit = card['suit']

        self.current_trick.append({'player_idx': player_idx, 'card': card})
        self.round_first_card_played = True
        self.bump_turn_token()
        self.clear_action_timer()

        if len(self.current_trick) == 4:
            self.is_processing_trick = True
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
            return True
        else:
            self.advance_to_next_active_player()
            return False

    def advance_to_next_active_player(self):
        active_players = [i for i, p in enumerate(self.players) if len(p['hand']) > 0]
        if not active_players:
            return

        current = self.current_turn
        while True:
            current = (current - 1) % 4
            if len(self.players[current]['hand']) > 0:
                self.current_turn = current
                break

    def finalize_trick(self):
        if len(self.current_trick) != 4:
            return False

        laugh_triggered = False
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
                laugh_triggered = True
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
        self.is_processing_trick = False
        self.bump_turn_token()
        self.clear_action_timer()

        if sum(p['tricks_won'] for p in self.players) == 13 or all(len(p['hand']) == 0 for p in self.players):
            game_ended = self.calculate_round_scores()
            if game_ended:
                self.state = 'GAME_OVER'
            else:
                self.state = 'ROUND_OVER'
                self.dealer_idx = (self.dealer_idx - 1) % 4
        else:
            self.state = 'PLAYING'

        return laugh_triggered

    def calculate_round_scores(self):
        self.round_summary = []
        bid_13_ended = False

        for p in self.players:
            bid = p['bid']
            won = p['tricks_won']
            ka_delta = p['king_ace_delta']
            round_pts = 0
            reason = ""

            if self.round_number == 1:
                round_pts = won
                reason = f"أخذ {won} أكلة (+{won})"
            else:
                if bid == 0:
                    if won == 0:
                        round_pts = 5
                        reason = "نجح في الصفر (+5)"
                    else:
                        round_pts = -5
                        reason = f"خسر الصفر بأكل {won} (-5)"
                elif bid in [7, 9]:
                    double_pts = bid * 2
                    if won >= bid:
                        round_pts = double_pts
                        reason = f"حقق طلب {bid} دبل (+{double_pts})"
                    else:
                        round_pts = -double_pts
                        reason = f"كسر طلب {bid} دبل (-{double_pts})"
                elif bid == 13:
                    bid_13_ended = True
                    if won == 13:
                        round_pts = 26
                        reason = "حقق طلب 13 وأنهى اللعبة (+26)"
                    else:
                        round_pts = -26
                        reason = "كسر طلب 13 وأنهى اللعبة (-26)"
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
            if ka_delta > 0:
                ka_note = " | أكل K♥ بـ A♥ (+5)"
            elif ka_delta < 0:
                ka_note = " | انأكل K♥ بـ A♥ (-5)"

            self.round_summary.append({
                'name': p['name'],
                'bid': bid if self.round_number > 1 else '-',
                'won': won,
                'change': total_round_change,
                'total': p['score'],
                'detail': reason + ka_note
            })

        max_score = max(p['score'] for p in self.players)
        min_score = min(p['score'] for p in self.players)
        leaders = [p['name'] for p in self.players if p['score'] == max_score]

        if max_score >= 31:
            self.winner_message = f"🏆 انتهت اللعبة! الفائز: {' و '.join(leaders)} (وصل 31)"
            return True
        if min_score <= -31:
            self.winner_message = f"🏆 انتهت اللعبة! الأعلى نقاطاً: {' و '.join(leaders)}"
            return True
        if bid_13_ended:
            self.winner_message = f"🏆 انتهت اللعبة بطلب 13! المتصدر: {' و '.join(leaders)}"
            return True

        return False


def resolve_auto_room_id(requested_room):
    req = (requested_room or '').strip()
    if req:
        return req

    for rid, r in ROOMS.items():
        if not r.game_started and len(r.players) < 4:
            return rid

    new_id = f"room_{random.randint(1000, 9999)}"
    while new_id in ROOMS:
        new_id = f"room_{random.randint(1000, 9999)}"
    return new_id


@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('join_game')
def on_join(data):
    room_id = resolve_auto_room_id(data.get('room'))
    player_name = (data.get('name') or 'لاعب').strip()
    join_room(room_id)

    if room_id not in ROOMS:
        ROOMS[room_id] = TarneebRoom(room_id)

    room = ROOMS[room_id]
    idx = room.add_player(player_name, sid=request.sid)
    if idx is None:
        emit('error', {'msg': 'الغرفة مكتملة!'})
        return

    if len(room.players) == 4 and not room.game_started:
        room.game_started = True
        room.waiting_timer_running = False
        room.start_new_round(increment_round=True)
        broadcast_state(room)
        socketio.start_background_task(run_game_loop, room)
        return

    broadcast_state(room)

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
        socketio.start_background_task(run_game_loop, room)


@socketio.on('force_start_bots')
def on_force_start(data):
    room = ROOMS.get(data.get('room'))
    if room and not room.game_started:
        room.fill_with_bots()
        room.game_started = True
        room.waiting_timer_running = False
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
            socketio.start_background_task(run_game_loop, room)


@socketio.on('next_round')
def on_next_round(data):
    room = ROOMS.get(data.get('room'))
    if room and room.state == 'ROUND_OVER':
        room.start_new_round(increment_round=True)
        broadcast_state(room)
        socketio.start_background_task(run_game_loop, room)


@socketio.on('submit_bid')
def on_bid(data):
    room = ROOMS.get(data.get('room'))
    if room and room.state == 'BIDDING':
        p_idx = get_player_idx_by_sid(room, request.sid)
        if p_idx == room.current_turn:
            room.process_bid(p_idx, int(data.get('bid', 2)))
            broadcast_state(room)
            socketio.start_background_task(run_game_loop, room)


@socketio.on('play_card')
def on_play_card(data):
    room = ROOMS.get(data.get('room'))
    if not room or room.is_processing_trick:
        return
    if room.state == 'PLAYING':
        p_idx = get_player_idx_by_sid(room, request.sid)
        if p_idx == room.current_turn and not room.players[p_idx]['is_bot']:
            card = data.get('card')
            valid, msg = room.validate_card_play(p_idx, card)
            if not valid:
                emit('error', {'msg': msg})
                return

            trick_done = room.play_card(p_idx, card)
            broadcast_state(room)

            if trick_done:
                socketio.start_background_task(handle_trick_pause_and_resume, room)
            else:
                socketio.start_background_task(run_game_loop, room)


def handle_trick_pause_and_resume(room):
    socketio.sleep(2.4)
    laugh_triggered = room.finalize_trick()
    if laugh_triggered:
        socketio.emit('play_laugh_sound', {'room': room.room_id}, to=room.room_id)
    broadcast_state(room)
    socketio.start_background_task(run_game_loop, room)


def human_timeout_watcher(room, expected_token, target_state, target_player_idx):
    while (
        room.game_started
        and room.state == target_state
        and room.turn_token == expected_token
        and room.current_turn == target_player_idx
    ):
        rem = int(max(0, round(room.action_deadline - time.time())))
        room.action_seconds_left = rem
        if rem <= 0:
            break
        broadcast_state(room)
        socketio.sleep(1)

    if (
        not room.game_started
        or room.state != target_state
        or room.turn_token != expected_token
        or room.current_turn != target_player_idx
    ):
        return

    if target_state == 'BIDDING':
        auto_bid = room.get_auto_timeout_bid(target_player_idx)
        room.process_bid(target_player_idx, auto_bid)
        broadcast_state(room)
        socketio.start_background_task(run_game_loop, room)
    elif target_state == 'PLAYING' and not room.is_processing_trick:
        p = room.players[target_player_idx]
        valid_cards = [c for c in p['hand'] if room.validate_card_play(target_player_idx, c)[0]]
        if valid_cards:
            chosen = room.select_smart_card(target_player_idx, valid_cards)
            trick_done = room.play_card(target_player_idx, chosen)
            broadcast_state(room)
            if trick_done:
                socketio.start_background_task(handle_trick_pause_and_resume, room)
            else:
                socketio.start_background_task(run_game_loop, room)


def run_game_loop(room):
    socketio.sleep(0.3)
    while room.game_started and room.state in ['BIDDING', 'PLAYING']:
        if all(len(p['hand']) == 0 for p in room.players):
            break

        p_idx = room.current_turn
        if p_idx >= len(room.players):
            break

        p = room.players[p_idx]

        if len(p['hand']) == 0 and room.state == 'PLAYING':
            room.advance_to_next_active_player()
            broadcast_state(room)
            socketio.sleep(0.2)
            continue

        if not p['is_bot']:
            timeout_sec = BID_TIMEOUT_SECONDS if room.state == 'BIDDING' else TURN_TIMEOUT_SECONDS
            room.set_action_timer(timeout_sec)
            token = room.turn_token
            broadcast_state(room)
            socketio.start_background_task(human_timeout_watcher, room, token, room.state, p_idx)
            break

        socketio.sleep(0.6)
        if room.state == 'BIDDING':
            smart_b = room.calculate_smart_bid(p_idx)
            room.process_bid(p_idx, smart_b)
            broadcast_state(room)
        elif room.state == 'PLAYING' and not room.is_processing_trick:
            valid_cards = [c for c in p['hand'] if room.validate_card_play(p_idx, c)[0]]
            if valid_cards:
                chosen = room.select_smart_card(p_idx, valid_cards)
                trick_done = room.play_card(p_idx, chosen)
                broadcast_state(room)
                if trick_done:
                    socketio.start_background_task(handle_trick_pause_and_resume, room)
                    break
        else:
            break


def get_player_idx_by_sid(room, sid):
    for p in room.players:
        if p['sid'] == sid:
            return p['idx']
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
                'action_seconds_left': room.action_seconds_left,
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
                    'bid': x['bid'] if room.round_number > 1 else '-',
                    'tricks_won': x['tricks_won'],
                    'won_cards_count': x['won_cards_count'],
                    'is_bot': x['is_bot'],
                    'cards_count': len(x['hand'])
                } for x in room.players]
            }
            socketio.emit('game_update', p_state, to=p['sid'])


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
