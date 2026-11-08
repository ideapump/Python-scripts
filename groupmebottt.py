import groupy
import time

client = groupy.Client.from_token('3GH5Ua55QuzfkYeTwhWrE4QVnWCs3TOoyTofnZiK')
groups = list(client.groups.list_all())
group = groups[0]
messages = group.messages.list()

#group.post(text='hi')

# for message in group.messages.list_all():
#      print(message[-1])

last_message = list(group.messages.list_all())[0]

if last_message.text == 'Get temp':
    group.post(text='Let me get that temp for you G')
    time.sleep(3)
    group.post(text=('Here it is: ' + '90 F'))

if 'Set temp' in last_message.text:
    n = 3
    set_temp = last_message.text[-n:]
    group.post(text='Starting hot tub and setting temp to' + ' ' + set_temp + '° F' )

if last_message.text == 'Turn off hot tub':
    group.post(text='Turning off Hot Tub')
    time.sleep(3)
    group.post(text=('Succesfully turned off.')) 



