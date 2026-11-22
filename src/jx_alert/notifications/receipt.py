"""Decode Feishu JSON without selecting a winner among contradictory fields."""


def strict_response_json(response):
    def unique(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError("duplicate JSON field")
            values[key] = value
        return values

    def invalid_constant(_):
        raise ValueError("invalid JSON constant")

    return response.json(object_pairs_hook=unique, parse_constant=invalid_constant)
