import subprocess as sp
import time

# Help Call
# sp.run(['python3', 'detect.py', '-h'])
epoch_list = [10]
batch_list = [10]
mp = [0]
g = [0]
count = 0
cmd_list = []
for i in epoch_list:
    for j in batch_list:
        for k in mp:
            for l in g:
                l1 = []
                l1.append('python3')
                l1.append('detect.py')
                l1.append('--train')
                l1.append('--test')
                l1.append('--verbose')
                l1.append('-e')
                l1.append(str(i))
                l1.append('-b')
                l1.append(str(j))
                if k:
                    l1.append('-mp')
                if l:
                    l1.append('-g')
                count += 1
                cmd_list.append(l1)

# print(cmd_list[-1],count)
# exit()
#cmds = [['python3', 'detect.py', '--train', '--epochs', '1', '--batch', '10', '--downsample', '500', '-g', '--verbose'],
#        ['python3', 'detect.py', '--train', '--epochs', '1', '--batch', '10', '--downsample', '500', '--verbose']]
cmds = [['python3', 'detect.py', '--train', '--epochs', '5', '--batch', '10', '--s', '1000', '-g', '1', '--verbose']]
start = time.time()
for cmd in cmds:
    print(cmd)
    p = sp.Popen(cmd)
    p.wait()

print("Time Taken (minutes): ",round((time.time() - start)/60,2))
