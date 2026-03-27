"""Tests for WeCom message encryption/decryption."""


from remote_control.wecom.crypto import (
    decrypt_message,
    encrypt_message,
    make_signature,
    parse_message_xml,
    verify_signature,
)

TEST_AES_KEY = "kWxPEV2UEDyxWpmPB8jfIqLfNjGjRiIpG2lMGKEQCTm"
TEST_CORP_ID = "test_corp_id"
TEST_TOKEN = "test_token"


def test_encrypt_then_decrypt():
    original = "Hello, this is a test message!"
    encrypted = encrypt_message(TEST_AES_KEY, TEST_CORP_ID, original)
    decrypted = decrypt_message(TEST_AES_KEY, encrypted)
    assert decrypted.content == original
    assert decrypted.corp_id == TEST_CORP_ID


def test_encrypt_decrypt_chinese():
    original = "你好，这是一条测试消息"
    encrypted = encrypt_message(TEST_AES_KEY, TEST_CORP_ID, original)
    decrypted = decrypt_message(TEST_AES_KEY, encrypted)
    assert decrypted.content == original


def test_encrypt_decrypt_empty_string():
    encrypted = encrypt_message(TEST_AES_KEY, TEST_CORP_ID, "")
    decrypted = decrypt_message(TEST_AES_KEY, encrypted)
    assert decrypted.content == ""
    assert decrypted.corp_id == TEST_CORP_ID


def test_encrypt_decrypt_long_message():
    original = "x" * 5000
    encrypted = encrypt_message(TEST_AES_KEY, TEST_CORP_ID, original)
    decrypted = decrypt_message(TEST_AES_KEY, encrypted)
    assert decrypted.content == original


def test_encrypt_produces_different_ciphertext():
    """Each encryption should produce different ciphertext due to random prefix."""
    msg = "same message"
    e1 = encrypt_message(TEST_AES_KEY, TEST_CORP_ID, msg)
    e2 = encrypt_message(TEST_AES_KEY, TEST_CORP_ID, msg)
    assert e1 != e2  # random prefix makes them different
    # But both decrypt to the same message
    assert decrypt_message(TEST_AES_KEY, e1).content == msg
    assert decrypt_message(TEST_AES_KEY, e2).content == msg


def test_verify_signature():
    timestamp = "1409659813"
    nonce = "nonce123"
    encrypt = "encrypted_string"
    sig = make_signature(TEST_TOKEN, timestamp, nonce, encrypt)
    assert verify_signature(TEST_TOKEN, timestamp, nonce, encrypt, sig)


def test_verify_signature_wrong_sig():
    assert not verify_signature(TEST_TOKEN, "ts", "nonce", "enc", "wrong_sig")


def test_verify_signature_wrong_token():
    sig = make_signature(TEST_TOKEN, "ts", "nonce", "enc")
    assert not verify_signature("wrong_token", "ts", "nonce", "enc", sig)


def test_make_signature_deterministic():
    s1 = make_signature("t", "1", "n", "e")
    s2 = make_signature("t", "1", "n", "e")
    assert s1 == s2


def test_make_signature_sorting():
    """Signature should be the same regardless of argument order in sort."""
    s1 = make_signature("a", "b", "c", "d")
    s2 = make_signature("d", "c", "b", "a")
    # Both should sort to [a, b, c, d] and produce the same hash
    assert s1 == s2


def test_parse_message_xml():
    xml = """<xml>
        <ToUserName><![CDATA[corp_id]]></ToUserName>
        <FromUserName><![CDATA[user1]]></FromUserName>
        <CreateTime>1348831860</CreateTime>
        <MsgType><![CDATA[text]]></MsgType>
        <Content><![CDATA[hello world]]></Content>
        <MsgId>1234567890</MsgId>
        <AgentID>1</AgentID>
        <Encrypt><![CDATA[encrypted_content]]></Encrypt>
    </xml>"""
    result = parse_message_xml(xml)
    assert result["FromUserName"] == "user1"
    assert result["Content"] == "hello world"
    assert result["MsgType"] == "text"
    assert result["Encrypt"] == "encrypted_content"
    assert result["CreateTime"] == "1348831860"
    assert result["AgentID"] == "1"


def test_parse_message_xml_minimal():
    xml = "<xml><Encrypt>data</Encrypt></xml>"
    result = parse_message_xml(xml)
    assert result["Encrypt"] == "data"
    assert "FromUserName" not in result


def test_parse_message_xml_empty_tags():
    xml = "<xml><Empty></Empty><HasText>value</HasText></xml>"
    result = parse_message_xml(xml)
    assert "Empty" not in result  # empty tags are skipped
    assert result["HasText"] == "value"
