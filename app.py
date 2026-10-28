import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'tarneeb_secret_key_libya_2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# تخزين الغرف في الذاكرة
# Structure: { room_code: { players: [], game_state: {} } }
ROOMS = {}

SUITS = ['H', 'D', 'C', 'S'] # H: Hearts (القلب الأحمر/الحاكم), D: Diamonds, C: Clubs, S: Spades
VALUES = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14] # 11: J, 12: Q, 13: K, 14: A (القص)

def create_deck():
    deck = []
    for s in SUITS:
        for v in VALUES:
            deck.append({'suit': s, 'value': v})
    random.shuffle(deck)
    return deck

def get_min_bid(score):
    if score >= 30: return 5
    if score >= 27: return 4
    if score >= 21: return 3
    return 2

class Game:
    def __init__(self, room_code):
        self.room_code = room_code
        self.players = [] # list of dicts: {'id': sid, 'name': name, 'is_bot': bool, 'score': 0, 'hand': [], 'bid': None, 'tricks_won': 0}
        self.dealer_idx = 0
        self.current_turn = 0
        self.phase = 'WAITING' # WAITING, FIRST_CARD, BIDDING, PLAYING, GAME_OVER
        self.current_trick = [] # [{'player_idx': i, 'card': card}]
        self.lead_suit = None
        self.total_bids = 0
        self.first_card_shown = None

    def add_player(self, sid, name, is_bot=False):
        if len(self.players) < 4:
            self.players.append({
                'id': sid,
                'name': name,
                'is_bot': is_bot,
                'score': 0,
                'hand': [],
                'bid': None,
                'tricks_won': 0
            })
            return True
        return False

    def fill_with_bots(self):
        bot_names = ["عمر (Bot)", "سالم (Bot)", "طارق (Bot)"]
        b_idx = 0
        while len(self.players) < 4:
            bot_id = f"bot_{random.randint(1000,9999)}"
            self.add_player(bot_id, bot_names[b_idx % len(bot_names)], is_bot=True)
            b_idx += 1

    def start_new_round(self):
        deck = create_deck()
        # فحص إمكانية إعادة التوزيع (إذا امتلك أي لاعب 7 ورقات من نفس النوع)
        while True:
            deck = create_deck()
            hands = [deck[i*12:(i+1)*12] for i in range(4)]
            redeal = False
            for h in hands:
                counts = {}
                for c in h:
                    counts[c['suit']] = counts.get(c['suit'], 0) + 1
                if any(cnt >= 7 for cnt in counts.values()):
                    redeal = True
                    break
            if not redeal:
                break

        for i in range(4):
            self.players[i]['hand'] = hands[i]
            self.players[i]['bid'] = None
            self.players[i]['tricks_won'] = 0

        # الورقة العشوائية الأولى
        self.first_card_shown = deck[48] # إحدى الأوراق المتبقية
        self.current_turn = (self.dealer_idx + 1) % 4
        self.phase = 'BIDDING'
        self.total_bids = 0
        self.current_trick = []
        self.lead_suit = None

    def process_bid(self, player_idx, bid_value):
        p = self.players[player_idx]
        min_b = get_min_bid(p['score'])
        
        # التأكد من الحد الأدنى
        if bid_value != 0 and bid_value < min_b and bid_value != 13:
            bid_value = min_b

        p['bid'] = bid_value
        self.total_bids += bid_value

        # التحقق من أن الموزع يتحمل العجز لو كان المجموع أقل من 11
        if player_idx == self.dealer_idx:
            if self.total_bids < 11:
                deficit = 11 - self.total_bids
                p['bid'] += deficit

        # التناوب إلى اللاعب التالي
        if all(p['bid'] is not None for p in self.players):
            self.phase = 'PLAYING'
            self.current_turn = (self.dealer_idx + 1) % 4
        else:
            self.current_turn = (self.current_turn + 1) % 4

    def play_card(self, player_idx, card):
        p = self.players[player_idx]
        
        # التأكد من صحة القانون: أول ورقة في أول شوط ليست قلب أحمر
        if len(self.current_trick) == 0 and self.lead_suit is None:
            # بداية أكله جديدة
            self.lead_suit = card['suit']
            
        # نزع الورقة من يد اللاعب
        p['hand'] = [c for c in p['hand'] if not (c['suit'] == card['suit'] and c['value'] == card['value'])]
        self.current_trick.append({'player_idx': player_idx, 'card': card})

        if len(self.current_trick) == 4:
            # تقييم الأكلة
            winner_idx = self.evaluate_trick()
            self.players[winner_idx]['tricks_won'] += 1
            self.current_turn = winner_idx
            trick_data = list(self.current_trick)
            self.current_trick = []
            self.lead_suit = None

            # التحقق من قانون القص والكينج الأحمر (Hearts)
            for item in trick_data:
                c = item['card']
                if c['suit'] == 'H' and c['value'] == 14: # القص الاحمر
                    # هل يوجد كينج أحمر في نفس الأكلة؟
                    has_king = any(t['card']['suit'] == 'H' and t['card']['value'] == 13 for t in trick_data)
                    if has_king:
                        self.players[item['player_idx']]['score'] += 5
                        for t in trick_data:
                            if t['card']['suit'] == 'H' and t['card']['value'] == 13:
                                self.players[t['player_idx']]['score'] -= 5

            # هل انتهت اليد (12 أكلة)؟
            if len(self.players[0]['hand']) == 0:
                self.calculate_scores()
                if not self.check_game_over():
                    self.dealer_idx = (self.dealer_idx + 1) % 4
                    self.start_new_round()
            return True
        else:
            self.current_turn = (self.current_turn + 1) % 4
            return False

    def evaluate_trick(self):
        # القلب الأحمر (H) هو الحاكم المستمر
        winning_card = None
        winner_idx = -1

        for item in self.current_trick:
            c = item['card']
            idx = item['player_idx']

            if winning_card is None:
                winning_card = c
                winner_idx = idx
            else:
                # إذا كانت الورقة كرت حاكم (H) والورقة الفائزة حالياً ليست H
                if c['suit'] == 'H' and winning_card['suit'] != 'H':
                    winning_card = c
                    winner_idx = idx
                # إذا كان كلاهما H أو كلاهما نفس نوع البداية (lead_suit)
                elif c['suit'] == winning_card['suit']:
                    if c['value'] > winning_card['value']:
                        winning_card = c
                        winner_idx = idx
        return winner_idx

    def calculate_scores(self):
        for p in self.players:
            bid = p['bid']
            won = p['tricks_won']
            
            if bid == 0:
                if won == 0:
                    p['score'] += 5
                else:
                    p['score'] -= 5
            elif bid == 7:
                if won >= 7: p['score'] += 14
                else: p['score'] -= 14
            elif bid == 9:
                if won >= 9: p['score'] += 21
                else: p['score'] -= 21
            elif bid == 13:
                if won == 13: p['score'] += 31
                else: p['score'] -= 31
            else:
                if won >= bid: p['score'] += bid
                else: p['score'] -= bid

    def check_game_over(self):
        for p in self.players:
            if p['score'] >= 31:
                self.phase = 'GAME_OVER'
                return True
        return False

    def bot_make_bid(self, bot_idx):
        p = self.players[bot_idx]
        min_b = get_min_bid(p['score'])
        # الذكاء الاصطناعي للبوت: حساب عدد الورق العالي والقلوب
        h_count = sum(1 for c in p['hand'] if c['suit'] == 'H')
        high_cards = sum(1 for c in p['hand'] if c['value'] >= 12)
        estimated = (h_count // 3) + high_cards
        bid = max(min_b, min(estimated, 5))
        self.process_bid(bot_idx, bid)

    def bot_play_card(self, bot_idx):
        p = self.players[bot_idx]
        hand = p['hand']
        
        # تصفية الأوراق المسموح بها حسب قانون إجبارية النوع
        valid_cards = []
        if self.lead_suit:
            same_suit = [c for c in hand if c['suit'] == self.lead_suit]
            if same_suit:
                valid_cards = same_suit
            else:
                valid_cards = hand
        else:
            # أول أكلة في الشوط ممنوع قلب أحمر إذا كان لديه أنواع أخرى
            non_hearts = [c for c in hand if c['suit'] != 'H']
            if non_hearts:
                valid_cards = non_hearts
            else:
                valid_cards = hand

        chosen_card = random.choice(valid_cards)
        self.play_card(bot_idx, chosen_card)

# --- مسارات Flask و Socket.IO ---

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('create_room')
def handle_create(data):
    room = str(random.randint(1000, 9999))
    ROOMS[room] = Game(room)
    ROOMS[room].add_player(request.sid, data['name'])
    join_room(room)
    emit('room_created', {'room': room, 'player_idx': 0})
    broadcast_state(room)

@socketio.on('join_room')
def handle_join(data):
    room = data['room']
    if room in ROOMS:
        game = ROOMS[room]
        if len(game.players) < 4:
            idx = len(game.players)
            game.add_player(request.sid, data['name'])
            join_room(room)
            emit('room_joined', {'room': room, 'player_idx': idx})
            broadcast_state(room)
        else:
            emit('error_msg', 'الغرفة مكتملة بالكامل!')
    else:
        emit('error_msg', 'رقم الغرفة غير موجود!')

@socketio.on('start_game')
def handle_start(data):
    room = data['room']
    if room in ROOMS:
        game = ROOMS[room]
        game.fill_with_bots() # تكميل باقي اللاعبين ببوتات مجانية
        game.start_new_round()
        broadcast_state(room)
        check_bot_turn(room)

@socketio.on('make_bid')
def handle_bid(data):
    room = data['room']
    game = ROOMS.get(room)
    if game and game.phase == 'BIDDING':
        game.process_bid(data['player_idx'], data['bid'])
        broadcast_state(room)
        check_bot_turn(room)

@socketio.on('play_card')
def handle_play(data):
    room = data['room']
    game = ROOMS.get(room)
    if game and game.phase == 'PLAYING':
        game.play_card(data['player_idx'], data['card'])
        broadcast_state(room)
        check_bot_turn(room)

def check_bot_turn(room):
    game = ROOMS.get(room)
    if not game: return
    
    # تنفيذ دور البوت تلقائياً بتأخير زمني بسيط للمحاكاة الواقعية
    while game.phase in ['BIDDING', 'PLAYING'] and game.players[game.current_turn]['is_bot']:
        eventlet.sleep(1)
        curr = game.current_turn
        if game.phase == 'BIDDING':
            game.bot_make_bid(curr)
        elif game.phase == 'PLAYING':
            game.bot_play_card(curr)
        broadcast_state(room)

def broadcast_state(room):
    game = ROOMS.get(room)
    if game:
        for idx, p in enumerate(game.players):
            if not p['is_bot']:
                # إرسال حالة اللعبة المخصصة لكل لاعب للحفاظ على سرية الورق
                state = {
                    'phase': game.phase,
                    'players': [{'name': pl['name'], 'score': pl['score'], 'bid': pl['bid'], 'tricks': pl['tricks_won']} for pl in game.players],
                    'my_hand': p['hand'],
                    'my_idx': idx,
                    'current_turn': game.current_turn,
                    'current_trick': game.current_trick,
                    'dealer_idx': game.dealer_idx,
                    'first_card': game.first_card_shown
                }
                socketio.emit('game_update', state, to=p['id'])

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)