import json
db_json_path = './data/MultiWOZ/'
db_list = ["attraction_db.json", "hospital_db.json", "hotel_db.json", "police_db.json", "restaurant_db.json",
           "taxi_db.json", "train_db.json"]
for db_name in db_list:
    with open(db_json_path + db_name, "r") as f:
        db_data = json.load(f)
    for num, data in enumerate(db_data):
        for key, value in data.items():
            if isinstance(value, int):
                db_data[num][key] = str(value)
            if isinstance(value, list):
                for n, item in enumerate(value):
                    value[n] = str(item)
                db_data[num][key] = ",".join(value)
            if isinstance(value, dict):
                v_list = []
                for v in value.values():
                    v_list.append(v)
                db_data[num][key] = ",".join(v_list)

    with open(db_json_path + db_name, "w") as f:
        json.dump(db_data, f, indent=4, separators=(",", ": "))
'''
data_path = './data/MultiWOZ/data_for_sequicity.json'
with open(data_path, "r") as f:
    data = json.load(f)
handle_data = []
for value in data.values():
    handle_data.append(value)
with open(data_path, "w") as f:
    json.dump(handle_data, f, indent=4, separators=(",", ": "))
'''


