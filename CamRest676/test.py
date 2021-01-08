import json
data_path = './data/MultiWOZ/data_for_sequicity.json'
with open(data_path, "r") as f:
    data = json.load(f)
request = {"[attraction]": [], "[hospital]": [], "[hotel]": [], "[police]": [], "[restaurant]": [],
           "[taxi]": [], "[train]": [], "[general]": []}
for value in data:
    for turn in value["log"]:
        domains = []
        for domain in turn["turn_domain"].split(" "):
            domains.append(domain)
        response = turn["resp"]
        for word in response.split(" "):
            if word:
                if word[0] == '[':
                    if word not in request[domain]:
                        request[domain].append(word)
req_path = './data/MultiWOZ/requirement.json'
with open(req_path, "w") as f:
    json.dump(request, f, indent=4, separators=(",", ": "))