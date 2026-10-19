import sys


def count_words(path):
    words = {}
    for line in open(path):
        for word in line.split():
            words[word] = words.get(word, 0) + 1
    return words


if __name__ == "__main__":
    for word, n in count_words(sys.argv[1]).items():
        print(word, n)
