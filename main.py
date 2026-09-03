from__future__importannotations

importasyncio
importcontextlib

fromfastapiimportFastAPI
fromcontextlibimportasynccontextmanager

fromaiogramimportBot,Dispatcher,F
fromaiogram.filtersimportCommand
fromaiogram.typesimportMessage,CallbackQuery

fromconfigimport(
BOT_TOKEN,
ADMIN_ID,
validate_config,
PAIRS,
MIN_PROBABILITY,
)

fromdatabaseimportdb

fromkeyboardsimport(
main_keyboard,
pending_keyboard,
admin_request_keyboard,
pair_selection_keyboard,
signal_type_keyboard,
regular_pair_selection_keyboard,
otc_pair_selection_keyboard,
all_pair_selection_keyboard,
expiry_selection_keyboard,
OTC_PAIRS,
format_pair,
)

fromschedulerimportSignalScheduler
frommarketimportmarket_client


#============================================================
#CONFIGVALIDATION
#============================================================

errors=validate_config()

iferrors:
print("[CONFIG]Ошибкиконфигурации:")

forerrorinerrors:
print(f"-{error}")


#============================================================
#BOT
#============================================================

bot=Bot(BOT_TOKEN)
dp=Dispatcher()


#============================================================
#SCHEDULER
#============================================================

scheduler=SignalScheduler(bot)

scheduler_task:asyncio.Task|None=None
polling_task:asyncio.Task|None=None


#============================================================
#USERSIGNALSELECTIONSTATE
#============================================================
#
#Послевыборапарыпользовательещёдолженвыбрать
#длительностьсделки.
#
#Структура:
#
#{
#user_id:{
#"pair":"EUR/USD",
#"pair_value":"EUR/USD",
#"name":"EUR/USD"
#}
#}
#
#Для"any_regular","any_otc"и"any"pairбудетNone.
#

pending_signal_selections:dict[int,dict]={}


#============================================================
#HELPERS
#============================================================

defis_approved(user_id:int)->bool:
user=db.get_user(user_id)

returnbool(
user
anduser["status"]=="APPROVED"
)


defsave_pending_selection(
user_id:int,
pair_value:str|None,
selected_name:str,
):
pending_signal_selections[user_id]={
"pair":pair_value,
"pair_value":pair_value,
"selected_name":selected_name,
}


defget_pending_selection(
user_id:int,
)->dict|None:
returnpending_signal_selections.get(user_id)


defclear_pending_selection(
user_id:int,
):
pending_signal_selections.pop(
user_id,
None,
)


#============================================================
#START
#============================================================

@dp.message(Command("start"))
asyncdefstart_handler(message:Message):

user_id=message.from_user.id
username=message.from_user.usernameor""
first_name=message.from_user.first_nameor""

#--------------------------------------------------------
#ADMIN
#--------------------------------------------------------

ifuser_id==ADMIN_ID:

db.create_or_update_user(
user_id=user_id,
username=username,
first_name=first_name,
status="APPROVED",
)

awaitmessage.answer(
"👑Панельадминистратора\n\n"
"Ботготовкработе.",
reply_markup=main_keyboard(),
)

return

#--------------------------------------------------------
#EXISTINGUSER
#--------------------------------------------------------

user=db.get_user(user_id)

ifuser:

status=user["status"]

#APPROVED

ifstatus=="APPROVED":

awaitmessage.answer(
"✅Доступразрешён.\n\n"
"Выберитедействие:",
reply_markup=main_keyboard(),
)

return

#PENDING

ifstatus=="PENDING":

awaitmessage.answer(
"⏳Вашазаявкаещёрассматривается.\n\n"
"Ожидайтеодобренияадминистратора.",
reply_markup=pending_keyboard(),
)

return

#REJECTED

ifstatus=="REJECTED":

awaitmessage.answer(
"❌Вдоступеотказано."
)

return

#BLOCKED

ifstatus=="BLOCKED":

awaitmessage.answer(
"🚫Вашдоступзаблокирован."
)

return

#--------------------------------------------------------
#NEWUSER
#--------------------------------------------------------

db.create_or_update_user(
user_id=user_id,
username=username,
first_name=first_name,
status="PENDING",
)

db.add_signal_request(user_id)

awaitmessage.answer(
"👋Заявкаотправленаадминистратору.\n\n"
"Послеодобрениятебестанетдоступен"
"генераторсигналов."
)

try:

awaitbot.send_message(
chat_id=ADMIN_ID,
text=(
"🔔Новаязаявканадоступ\n\n"
f"👤Имя:{first_name}\n"
f"🔗Username:@{usernameifusernameelse'нет'}\n"
f"🆔ID:{user_id}"
),
reply_markup=admin_request_keyboard(user_id),
)

exceptExceptionasexc:

print(
f"[ADMIN]Ошибкауведомления:{exc}"
)


#============================================================
#CHECKACCESS
#============================================================

@dp.callback_query(F.data=="check_access")
asyncdefcheck_access_callback(
callback:CallbackQuery,
):

user_id=callback.from_user.id

user=db.get_user(user_id)

ifnotuser:

awaitcallback.answer(
"Заявканенайдена.",
show_alert=True,
)

return

status=user["status"]

#APPROVED

ifstatus=="APPROVED":

awaitcallback.answer()

awaitcallback.message.edit_text(
"✅Доступужеодобрен.\n\n"
"Выберитедействие:",
reply_markup=main_keyboard(),
)

return

#PENDING

ifstatus=="PENDING":

awaitcallback.answer(
"⏳Заявкаещёрассматривается.",
show_alert=True,
)

return

#BLOCKED

ifstatus=="BLOCKED":

awaitcallback.answer(
"🚫Доступзаблокирован.",
show_alert=True,
)

return

#REJECTED

awaitcallback.answer(
"❌Доступнепредоставлен.",
show_alert=True,
)


#============================================================
#ADMINAPPROVE
#============================================================

@dp.callback_query(F.data.startswith("approve:"))
asyncdefapprove_callback(
callback:CallbackQuery,
):

ifcallback.from_user.id!=ADMIN_ID:

awaitcallback.answer(
"Нетдоступа.",
show_alert=True,
)

return

try:

user_id=int(
callback.data.split(
":",
1,
)[1]
)

except(ValueError,IndexError):

awaitcallback.answer(
"НекорректныйIDпользователя.",
show_alert=True,
)

return

db.set_status(
user_id=user_id,
status="APPROVED",
)

awaitcallback.answer(
"Пользовательодобрен."
)

try:

awaitbot.send_message(
chat_id=user_id,
text=(
"🎉Доступодобрен!\n\n"
"Теперьтебедоступнысигналы."
),
reply_markup=main_keyboard(),
)

exceptExceptionasexc:

print(
f"[ADMIN]Ошибкауведомленияпользователя:{exc}"
)

withcontextlib.suppress(Exception):

awaitcallback.message.edit_reply_markup(
reply_markup=None
)


#============================================================
#ADMINREJECT
#============================================================

@dp.callback_query(F.data.startswith("reject:"))
asyncdefreject_callback(
callback:CallbackQuery,
):

ifcallback.from_user.id!=ADMIN_ID:

awaitcallback.answer(
"Нетдоступа.",
show_alert=True,
)

return

try:

user_id=int(
callback.data.split(
":",
1,
)[1]
)

except(ValueError,IndexError):

awaitcallback.answer(
"НекорректныйIDпользователя.",
show_alert=True,
)

return

db.set_status(
user_id=user_id,
status="REJECTED",
)

awaitcallback.answer(
"Пользовательотклонён."
)

try:

awaitbot.send_message(
chat_id=user_id,
text="❌Вашазаявканадоступотклонена.",
)

exceptExceptionasexc:

print(
f"[ADMIN]Ошибкауведомленияпользователя:{exc}"
)

withcontextlib.suppress(Exception):

awaitcallback.message.edit_reply_markup(
reply_markup=None
)


#============================================================
#GETSIGNAL
#============================================================

@dp.callback_query(F.data=="request_signal")
asyncdefrequest_signal_callback(
callback:CallbackQuery,
):

user=db.get_user(
callback.from_user.id
)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Утебянетдоступа.",
show_alert=True,
)

return

clear_pending_selection(
callback.from_user.id
)

awaitcallback.answer()

awaitcallback.message.edit_text(
"🎯Получениесигнала\n\n"
"Выберитипрынка:\n\n"
"💱Обычныепары—обычныйForex\n"
"🟣OTC—OTC-пары\n"
"🔀Всепары—обычные+OTC",
reply_markup=signal_type_keyboard(),
)


#============================================================
#SIGNALTYPE:REGULAR
#============================================================

@dp.callback_query(F.data=="signal_type:regular")
asyncdefsignal_type_regular_callback(
callback:CallbackQuery,
):

user=db.get_user(
callback.from_user.id
)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Нетдоступа.",
show_alert=True,
)

return

clear_pending_selection(
callback.from_user.id
)

awaitcallback.answer()

awaitcallback.message.edit_text(
"💱Обычныевалютныепары\n\n"
f"📈Минимальныйшанс:"
f"{MIN_PROBABILITY:.0f}%\n\n"
"Выберипару:",
reply_markup=regular_pair_selection_keyboard(),
)


#============================================================
#SIGNALTYPE:OTC
#============================================================

@dp.callback_query(F.data=="signal_type:otc")
asyncdefsignal_type_otc_callback(
callback:CallbackQuery,
):

user=db.get_user(
callback.from_user.id
)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Нетдоступа.",
show_alert=True,
)

return

clear_pending_selection(
callback.from_user.id
)

awaitcallback.answer()

awaitcallback.message.edit_text(
"🟣OTCвалютныепары\n\n"
f"📈Минимальныйшанс:"
f"{MIN_PROBABILITY:.0f}%\n\n"
"ВыбериOTC-пару:",
reply_markup=otc_pair_selection_keyboard(),
)


#============================================================
#SIGNALTYPE:ALL
#============================================================

@dp.callback_query(F.data=="signal_type:all")
asyncdefsignal_type_all_callback(
callback:CallbackQuery,
):

user=db.get_user(
callback.from_user.id
)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Нетдоступа.",
show_alert=True,
)

return

clear_pending_selection(
callback.from_user.id
)

awaitcallback.answer()

awaitcallback.message.edit_text(
"🔀Вседоступныепары\n\n"
"💱Обычные+🟣OTC\n\n"
f"📈Минимальныйшанс:"
f"{MIN_PROBABILITY:.0f}%\n\n"
"Выберипаруилиавтоматическийпоиск:",
reply_markup=all_pair_selection_keyboard(),
)


#============================================================
#SIGNALTYPE:BACK
#============================================================

@dp.callback_query(F.data=="signal_type:back")
asyncdefsignal_type_back_callback(
callback:CallbackQuery,
):

user=db.get_user(
callback.from_user.id
)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Нетдоступа.",
show_alert=True,
)

return

clear_pending_selection(
callback.from_user.id
)

awaitcallback.answer()

awaitcallback.message.edit_text(
"🎯Получениесигнала\n\n"
"Выберитипрынка:\n\n"
"💱Обычныепары\n"
"🟣OTCпары\n"
"🔀Всепары",
reply_markup=signal_type_keyboard(),
)


#============================================================
#SIGNALTYPE:CANCEL
#============================================================

@dp.callback_query(F.data=="signal_type:cancel")
asyncdefsignal_type_cancel_callback(
callback:CallbackQuery,
):

user=db.get_user(
callback.from_user.id
)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Нетдоступа.",
show_alert=True,
)

return

clear_pending_selection(
callback.from_user.id
)

awaitcallback.answer()

awaitcallback.message.edit_text(
"❌Получениесигналаотменено.",
reply_markup=main_keyboard(),
)


#============================================================
#PAIRSELECTION
#============================================================

@dp.callback_query(F.data.startswith("pair:"))
asyncdefpair_callback(
callback:CallbackQuery,
):

user_id=callback.from_user.id

user=db.get_user(user_id)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Нетдоступа.",
show_alert=True,
)

return

pair_value=callback.data.split(
":",
1,
)[1]

#--------------------------------------------------------
#CANCEL
#--------------------------------------------------------

ifpair_value=="cancel":

clear_pending_selection(user_id)

awaitcallback.answer()

awaitcallback.message.edit_text(
"❌Получениесигналаотменено.",
reply_markup=main_keyboard(),
)

return

#--------------------------------------------------------
#ANYPAIR
#--------------------------------------------------------

ifpair_value=="any":

selected_pair=None
selected_name="Любаяпара"

#--------------------------------------------------------
#ANYREGULAR
#--------------------------------------------------------

elifpair_value=="any_regular":

selected_pair=None
selected_name="Любаяобычнаяпара"

#--------------------------------------------------------
#ANYOTC
#--------------------------------------------------------

elifpair_value=="any_otc":

selected_pair=None
selected_name="ЛюбаяOTCпара"

#--------------------------------------------------------
#SPECIFICPAIR
#--------------------------------------------------------

else:

selected_pair=pair_value

is_regular=(
selected_pairinPAIRS
)

is_otc=(
selected_pairinOTC_PAIRS
)

ifnotis_regularandnotis_otc:

awaitcallback.answer(
"❌Неизвестнаяпара.",
show_alert=True,
)

return

selected_name=format_pair(
selected_pair
)

#--------------------------------------------------------
#SAVESELECTION
#--------------------------------------------------------

save_pending_selection(
user_id=user_id,
pair_value=selected_pair,
selected_name=selected_name,
)

#--------------------------------------------------------
#SHOWEXPIRYMENU
#--------------------------------------------------------

awaitcallback.answer()

awaitcallback.message.edit_text(
"⏱️Выборвременисделки\n\n"
f"💱{selected_name}\n\n"
"Выберидлительностьсделки:\n\n"
"От1до20минут.\n"
"Илинажми«⚡Любоевремя»,"
"чтобысистемапровериладоступныеварианты.",
reply_markup=expiry_selection_keyboard(),
)


#============================================================
#EXPIRY:BACK
#============================================================

@dp.callback_query(F.data=="expiry:back")
asyncdefexpiry_back_callback(
callback:CallbackQuery,
):

user_id=callback.from_user.id

user=db.get_user(user_id)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Нетдоступа.",
show_alert=True,
)

return

clear_pending_selection(user_id)

awaitcallback.answer()

awaitcallback.message.edit_text(
"🎯Получениесигнала\n\n"
"Выберитипрынка:\n\n"
"💱Обычныепары\n"
"🟣OTCпары\n"
"🔀Всепары",
reply_markup=signal_type_keyboard(),
)


#============================================================
#EXPIRY:CANCEL
#============================================================

@dp.callback_query(F.data=="expiry:cancel")
asyncdefexpiry_cancel_callback(
callback:CallbackQuery,
):

user_id=callback.from_user.id

user=db.get_user(user_id)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Нетдоступа.",
show_alert=True,
)

return

clear_pending_selection(user_id)

awaitcallback.answer()

awaitcallback.message.edit_text(
"❌Получениесигналаотменено.",
reply_markup=main_keyboard(),
)


#============================================================
#EXPIRYSELECTION
#============================================================

@dp.callback_query(F.data.startswith("expiry:"))
asyncdefexpiry_callback(
callback:CallbackQuery,
):

user_id=callback.from_user.id

user=db.get_user(user_id)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Нетдоступа.",
show_alert=True,
)

return

expiry_value=callback.data.split(
":",
1,
)[1]

#--------------------------------------------------------
#BACK
#--------------------------------------------------------

ifexpiry_value=="back":
return

#--------------------------------------------------------
#CANCEL
#--------------------------------------------------------

ifexpiry_value=="cancel":
return

#--------------------------------------------------------
#VALIDATEDURATION
#--------------------------------------------------------

ifexpiry_value=="any":

expiry_minutes:int|str="any"

expiry_text="Любоевремя"

else:

try:

expiry_minutes=int(
expiry_value
)

except(ValueError,TypeError):

awaitcallback.answer(
"❌Некорректноевремя.",
show_alert=True,
)

return

ifnot1<=expiry_minutes<=20:

awaitcallback.answer(
"❌Времядолжнобытьот1до20минут.",
show_alert=True,
)

return

expiry_text=f"{expiry_minutes}мин."


#--------------------------------------------------------
#GETSAVEDPAIR
#--------------------------------------------------------

selection=get_pending_selection(
user_id
)

ifnotselection:

awaitcallback.answer(
"⚠️Выборпарыустарел.Начнизаново.",
show_alert=True,
)

withcontextlib.suppress(Exception):

awaitcallback.message.edit_text(
"⚠️Выборсигналаустарел.\n\n"
"Начниполучениесигналазаново.",
reply_markup=main_keyboard(),
)

return

selected_pair=selection["pair"]
selected_name=selection["selected_name"]

#--------------------------------------------------------
#CALLBACK
#--------------------------------------------------------

awaitcallback.answer(
"🔎Начинаюанализ..."
)

#--------------------------------------------------------
#STATUS
#--------------------------------------------------------

awaitcallback.message.edit_text(
"🔎Анализируюрынок...\n\n"
f"💱{selected_name}\n"
f"⏱️Времясделки:{expiry_text}\n"
f"📈Минимальныйшанс:"
f"{MIN_PROBABILITY:.0f}%\n\n"
"⏳Проверяюрынок..."
)

#--------------------------------------------------------
#MARKETANALYSIS
#--------------------------------------------------------

try:

#====================================================
#SPECIFICPAIR
#====================================================

ifselected_pairisnotNone:

signal=awaitscheduler.get_manual_signal(
pair=selected_pair,
expiry_minutes=expiry_minutes,
)

#====================================================
#ANYREGULAR
#====================================================

elif(
selection["pair_value"]
isNone
andselected_name=="Любаяобычнаяпара"
):

signal=None
best_signal=None

forpairinPAIRS:

try:

candidate=(
awaitscheduler.get_manual_signal(
pair=pair,
expiry_minutes=expiry_minutes,
)
)

ifcandidateisNone:
continue

ifbest_signalisNone:

best_signal=candidate

elif(
candidate.quality
>best_signal.quality
):

best_signal=candidate

elif(
candidate.quality
==best_signal.quality
andcandidate.probability
>best_signal.probability
):

best_signal=candidate

exceptExceptionasexc:

print(
f"[MANUAL]Ошибка"
f"{pair}:"
f"{type(exc).__name__}:"
f"{exc}"
)

signal=best_signal

#====================================================
#ANYOTC
#====================================================

elif(
selection["pair_value"]
isNone
andselected_name=="ЛюбаяOTCпара"
):

signal=None
best_signal=None

forpairinOTC_PAIRS:

try:

candidate=(
awaitscheduler.get_manual_signal(
pair=pair,
expiry_minutes=expiry_minutes,
)
)

ifcandidateisNone:
continue

ifbest_signalisNone:

best_signal=candidate

elif(
candidate.quality
>best_signal.quality
):

best_signal=candidate

elif(
candidate.quality
==best_signal.quality
andcandidate.probability
>best_signal.probability
):

best_signal=candidate

exceptExceptionasexc:

print(
f"[MANUALOTC]Ошибка"
f"{pair}:"
f"{type(exc).__name__}:"
f"{exc}"
)

signal=best_signal

#====================================================
#ANYALL
#====================================================

else:

signal=awaitscheduler.get_manual_signal(
pair=None,
expiry_minutes=expiry_minutes,
)

exceptTypeErrorasexc:

#----------------------------------------------------
#BACKWARDCOMPATIBILITY
#----------------------------------------------------
#
#Еслипокакой-топричиненаRenderосталсястарый
#scheduler.pyбезпараметраexpiry_minutes,непадаем
#сTypeError,апробуемстарыйвызов.
#
#----------------------------------------------------

print(
"[MANUAL]Schedulerнеподдерживает"
f"expiry_minutes:{exc}"
)

try:

ifselected_pairisnotNone:

signal=awaitscheduler.get_manual_signal(
pair=selected_pair,
)

elif(
selected_name=="Любаяобычнаяпара"
):

signal=None
best_signal=None

forpairinPAIRS:

try:

candidate=(
awaitscheduler.get_manual_signal(
pair=pair
)
)

ifcandidateisNone:
continue

if(
best_signalisNone
orcandidate.quality
>best_signal.quality
):

best_signal=candidate

exceptExceptionasinner_exc:

print(
f"[MANUAL]Ошибка"
f"{pair}:"
f"{type(inner_exc).__name__}:"
f"{inner_exc}"
)

signal=best_signal

elif(
selected_name=="ЛюбаяOTCпара"
):

signal=None
best_signal=None

forpairinOTC_PAIRS:

try:

candidate=(
awaitscheduler.get_manual_signal(
pair=pair
)
)

ifcandidateisNone:
continue

if(
best_signalisNone
orcandidate.quality
>best_signal.quality
):

best_signal=candidate

exceptExceptionasinner_exc:

print(
f"[MANUALOTC]Ошибка"
f"{pair}:"
f"{type(inner_exc).__name__}:"
f"{inner_exc}"
)

signal=best_signal

else:

signal=awaitscheduler.get_manual_signal(
pair=None
)

exceptExceptionasfallback_exc:

print(
"[MANUAL]Ошибкаfallback:"
)

print(
f"{type(fallback_exc).__name__}:"
f"{fallback_exc}"
)

clear_pending_selection(
user_id
)

awaitcallback.message.edit_text(
"⚠️Неудалосьполучитьсигнал.\n\n"
f"💱{selected_name}\n"
f"⏱️Времясделки:{expiry_text}\n\n"
"Произошлаошибкаприанализерынка.",
reply_markup=main_keyboard(),
)

return

exceptExceptionasexc:

print(
"[MANUAL]Ошибкаполучениясигнала:"
)

print(
f"{type(exc).__name__}:{exc}"
)

clear_pending_selection(
user_id
)

awaitcallback.message.edit_text(
"⚠️Неудалосьполучитьсигнал.\n\n"
f"💱{selected_name}\n"
f"⏱️Времясделки:{expiry_text}\n\n"
"Произошлаошибкаприанализерынка.",
reply_markup=main_keyboard(),
)

return

#--------------------------------------------------------
#CLEARTEMPORARYSELECTION
#--------------------------------------------------------

clear_pending_selection(
user_id
)

#--------------------------------------------------------
#NOSIGNAL
#--------------------------------------------------------

ifsignalisNone:

text=(
"⚪Сильногосигналасейчаснет.\n\n"
f"💱{selected_name}\n"
f"⏱️Времясделки:{expiry_text}\n"
f"📈Минимальныйшанс:"
f"{MIN_PROBABILITY:.0f}%\n\n"
"Янебудувыдаватьслабыйсигнал"
"толькорадитого,чтобычто-топоказать."
)

awaitcallback.message.edit_text(
text,
reply_markup=main_keyboard(),
)

return

#--------------------------------------------------------
#SIGNALFOUND
#--------------------------------------------------------

text=scheduler.format_signal(
signal
)

awaitcallback.message.edit_text(
text,
reply_markup=main_keyboard(),
)


#============================================================
#HISTORY
#============================================================

@dp.callback_query(F.data=="history")
asyncdefhistory_callback(
callback:CallbackQuery,
):

user=db.get_user(
callback.from_user.id
)

ifnotuseroruser["status"]!="APPROVED":

awaitcallback.answer(
"❌Нетдоступа.",
show_alert=True,
)

return

awaitcallback.answer()

signals=db.get_recent_signals(
limit=10
)

ifnotsignals:

awaitcallback.message.edit_text(
"📊Историяпокапустая.",
reply_markup=main_keyboard(),
)

return

lines=[
"📊ПОСЛЕДНИЕСИГНАЛЫ\n"
]

forsignalinsignals:

direction=signal["direction"]

emoji=(
"🟢"
ifdirection=="CALL"
else"🔴"
)

result=(
signal["result"]
or"—"
)

lines.append(
f"{emoji}{direction}|"
f"{signal['pair']}|"
f"Q:{signal['quality']}|"
f"{result}"
)

awaitcallback.message.edit_text(
"\n".join(lines),
reply_markup=main_keyboard(),
)


#============================================================
#/USERS
#============================================================

@dp.message(Command("users"))
asyncdefusers_handler(
message:Message,
):

ifmessage.from_user.id!=ADMIN_ID:
return

users=db.get_pending_users()

ifnotusers:

awaitmessage.answer(
"📭Новыхзаявокнет."
)

return

foruserinusers:

user_id=int(
user["user_id"]
)

username=(
user["username"]
or"нет"
)

first_name=(
user["first_name"]
or"нет"
)

awaitmessage.answer(
"👤Заявка\n\n"
f"Имя:{first_name}\n"
f"Username:@{username}\n"
f"ID:{user_id}",
reply_markup=admin_request_keyboard(
user_id
),
)


#============================================================
#/ID
#============================================================

@dp.message(Command("id"))
asyncdefid_handler(
message:Message,
):

awaitmessage.answer(
"🆔ТвойTelegramID:\n\n"
f"{message.from_user.id}"
)


#============================================================
#FALLBACK
#============================================================

@dp.message()
asyncdeffallback_handler(
message:Message,
):

user=db.get_user(
message.from_user.id
)

ifuseranduser["status"]=="APPROVED":

awaitmessage.answer(
"Выберидействие:",
reply_markup=main_keyboard(),
)

elifuseranduser["status"]=="PENDING":

awaitmessage.answer(
"⏳Заявкаещёрассматривается.",
reply_markup=pending_keyboard(),
)

elifuseranduser["status"]=="BLOCKED":

awaitmessage.answer(
"🚫Вашдоступзаблокирован."
)

else:

awaitmessage.answer(
"Используй/start"
)


#============================================================
#FASTAPILIFESPAN
#============================================================

@asynccontextmanager
asyncdeflifespan(
app:FastAPI,
):

globalscheduler_task
globalpolling_task

print(
"[APP]ЗапускPocketSignalBot..."
)

#--------------------------------------------------------
#SETBOT
#--------------------------------------------------------

try:

scheduler.set_bot(bot)

exceptException:

pass

#--------------------------------------------------------
#STARTSCHEDULER
#--------------------------------------------------------

scheduler_task=asyncio.create_task(
scheduler.run()
)

#--------------------------------------------------------
#STARTPOLLING
#--------------------------------------------------------

polling_task=asyncio.create_task(
dp.start_polling(
bot,
handle_signals=True,
)
)

yield

print(
"[APP]Остановка..."
)

#--------------------------------------------------------
#STOPSCHEDULER
#--------------------------------------------------------

ifscheduler_task:

scheduler_task.cancel()

withcontextlib.suppress(
asyncio.CancelledError
):

awaitscheduler_task

#--------------------------------------------------------
#STOPPOLLING
#--------------------------------------------------------

ifpolling_task:

polling_task.cancel()

withcontextlib.suppress(
asyncio.CancelledError
):

awaitpolling_task

#--------------------------------------------------------
#CLOSEMARKET
#--------------------------------------------------------

withcontextlib.suppress(
Exception
):

awaitmarket_client.close()

#--------------------------------------------------------
#CLOSEBOT
#--------------------------------------------------------

withcontextlib.suppress(
Exception
):

awaitbot.session.close()


#============================================================
#FASTAPIAPP
#============================================================

app=FastAPI(
lifespan=lifespan
)


#============================================================
#ROOT
#============================================================

@app.get("/")
asyncdefroot():

return{
"status":"ok",
"service":"PocketSignalBot",
}


#============================================================
#HEALTH
#============================================================

@app.get("/health")
asyncdefhealth():

return{
"status":"healthy",
}
