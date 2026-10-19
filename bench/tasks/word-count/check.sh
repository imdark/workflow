printf 'The cat, the dog.\nA dog!\n' > in.txt
out=$(python3 count.py in.txt)
expected=$(printf 'dog 2\nthe 2\na 1\ncat 1')
[ "$out" = "$expected" ]
