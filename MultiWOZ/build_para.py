import json
import argparse
from para_analysis import build_delex_group_multiwoz, find_para_multiwoz

parser = argparse.ArgumentParser()
parser.add_argument("-in", "--input_file", type=str, default='data/multi-woz-processed/data_for_damd.json',
                    help="input original data")
parser.add_argument("-f", "--output_file", type=str, default='data/multi-woz-processed/data_with_para.json',
                    help="output data with initial utterance paraphrase and group id")
parser.add_argument("-para", "--paraphrase_file", type=str, default='data/multi-woz-processed/database_para.json',
                    help="output paraphrase database for reference")
parser.add_argument("-bleu", "--bleu_threshold", type=float, default=0.2,
                    help="the bleu score threshold for filtering in random selection")
parser.add_argument("-diversity", "--diversity_threshold", type=float, default=3.4,
                    help="the diversity score threshold for filtering in random selection")
args = parser.parse_args()

if __name__ == '__main__':
    with open(args.input_file, 'r') as f_input:
        data = json.load(f_input)

    dial_name = []
    dial_content = []
    for name, content in data.items():
        dial_name.append(name)
        dial_content.append(content)

    for number, dialogue in enumerate(dial_content):
        for count, dial_turn in enumerate(dial_content[number]["log"]):
            dial_content[number]["log"][count]["para"] = ""
            dial_content[number]["log"][count]["para_delex"] = ""
            dial_content[number]["log"][count]["context"] = {}
            dial_content[number]["log"][count]["group"] = -1

    dial_content = build_delex_group_multiwoz(dial_content, args.paraphrase_file)
    dial_content = find_para_multiwoz(dial_content, args.paraphrase_file, args.diversity_threshold, args.bleu_threshold)

    for count, name in enumerate(dial_name):
        data[name] = dial_content[count]

    with open(args.output_file, 'w') as f_out:
        json.dump(data, f_out, indent=4, separators=(",", ": "))
