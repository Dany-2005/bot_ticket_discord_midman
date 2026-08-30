import os
import io
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Nama role yang boleh klik "Konfirmasi Pembayaran" & "Set Tax".
# Buat role ini di server kamu (Server Settings > Roles) lalu pasang ke akun kamu sendiri.
MIDDLEMAN_ROLE_NAME = os.getenv("MIDDLEMAN_ROLE_NAME", "Middleman")

# Path gambar QRIS lokal (taruh file di folder assets/ sejajar dengan bot.py)
QRIS_IMAGE_PATH = "assets/qris.png"

# Persentase fee middleman DEFAULT (cuma dipakai sebagai nilai awal di popup
# input tax & di /mm calculator kalau kamu belum override). Tax yang beneran
# dipakai buat hitung transaksi selalu diisi manual oleh Middleman lewat
# tombol "Set Tax" di tiap tiket -- jadi mau ganti persen, gak perlu edit
# kode / restart bot lagi.
MM_FEE_PERCENT = 3

# Nama channel tempat user bisa cek ulang perhitungan tax secara manual
TAX_CHANNEL_NAME = os.getenv("TAX_CHANNEL_NAME", "midman-tax")

# Nama channel tempat bot otomatis posting proof/testimoni setelah transaksi selesai
TESTIMONI_CHANNEL_NAME = os.getenv("TESTIMONI_CHANNEL_NAME", "testimoni")

# Potongan admin buat pencairan dana ke Seller, tergantung metode yang dipilih
# Seller. Ini DIPOTONG dari harga barang (Seller nerima harga - potongan).
# Ubah nominal di sini kalau tarif admin e-wallet kamu berubah.
PAYOUT_ADMIN_FEE = {
    "DANA": 1000,
    "GoPay": 0,
    "QRIS": 0,
}

# Path folder font (dipakai buat generate gambar kartu proof/testimoni)
FONT_DIR = "assets/fonts"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True


class TicketBot(commands.Bot):

  def __init__(self):
    super().__init__(command_prefix="!", intents=intents)

  async def setup_hook(self):
    await self.tree.sync()
    print("Slash commands berhasil disinkronkan secara global!")

  async def on_guild_join(self, guild: discord.Guild):
    self.tree.copy_global_to(guild=guild)
    await self.tree.sync(guild=guild)


bot = TicketBot()

# Simpan data tiket sementara di memory, keyed by channel id.
# Kalau bot restart data ini hilang -- kalau butuh persist antar-restart,
# ganti dict ini dengan baca/tulis ke file JSON atau database.
TICKET_DATA: dict[int, dict] = {}


def is_middleman(member: discord.Member) -> bool:
  if member.guild_permissions.administrator:
    return True
  return any(role.name == MIDDLEMAN_ROLE_NAME for role in member.roles)


def hitung_fee(harga: int, persen: float = MM_FEE_PERCENT) -> tuple[int, int]:
  """Return (fee, total_transfer) dari harga barang, pakai persentase tax
  yang dikasih (default ke MM_FEE_PERCENT kalau gak dikasih, misal buat /mm)."""
  fee = round(harga * persen / 100)
  total = harga + fee
  return fee, total


def cari_channel(guild: discord.Guild, keyword: str) -> discord.TextChannel | None:
  """Cari text channel yang NAMANYA MENGANDUNG keyword (case-insensitive),
  bukan harus sama persis. Ini penting karena banyak server kasih emoji di
  depan nama channel (misal '📸・testimoni'), jadi exact-match nama gak
  bakal ketemu walau channel-nya ada."""
  keyword = keyword.lower()
  for channel in guild.text_channels:
    if keyword in channel.name.lower():
      return channel
  return None


def buat_embed_dasar(
    guild: discord.Guild,
    title: str,
    description: str = None,
    color: discord.Color = discord.Color.blue(),
) -> discord.Embed:
  """Bikin embed dengan tampilan konsisten -- kasih thumbnail ikon server
  (kalau ada) & timestamp otomatis, biar semua embed bot kelihatan seragam
  dan lebih profesional."""
  embed = discord.Embed(title=title, description=description, color=color)
  if guild.icon:
    embed.set_thumbnail(url=guild.icon.url)
  embed.timestamp = discord.utils.utcnow()
  return embed


# ---------- Step 1: Pilih partner + tentukan siapa Buyer (dropdown, bukan modal) ----------
class SetupView(discord.ui.View):
  """Muncul begitu tombol 'Order Middleman' diklik. User wajib pilih partner
  DAN nentuin siapa Buyer sebelum lanjut isi nama item & harga.
  Gak ada opsi skip -- tombol 'Lanjut' baru aktif kalau dua-duanya udah diisi."""

  def __init__(self, ticket_owner: discord.Member):
    super().__init__(timeout=180)
    self.ticket_owner = ticket_owner
    self.partner: discord.Member | None = None
    self.buyer_choice: str | None = None  # "owner" atau "partner"

  def _is_ticket_owner(self, interaction: discord.Interaction) -> bool:
    return interaction.user.id == self.ticket_owner.id

  @discord.ui.select(
      cls=discord.ui.UserSelect,
      placeholder="1️⃣ Pilih user lawan transaksi (buyer/seller)",
      min_values=1,
      max_values=1,
      row=0,
  )
  async def select_partner(
      self, interaction: discord.Interaction, select: discord.ui.UserSelect
  ):
    if not self._is_ticket_owner(interaction):
      await interaction.response.send_message(
          "❌ Cuma pembuat tiket yang bisa pilih partner ini.", ephemeral=True
      )
      return

    partner = select.values[0]
    if partner.id == self.ticket_owner.id:
      await interaction.response.send_message(
          "❌ Gak bisa pilih diri sendiri sebagai partner.", ephemeral=True
      )
      return

    self.partner = partner
    await interaction.response.defer()

  @discord.ui.select(
      placeholder="2️⃣ Siapa yang jadi Buyer di transaksi ini?",
      min_values=1,
      max_values=1,
      row=1,
      options=[
          discord.SelectOption(
              label="Saya (pembuat tiket) sebagai Buyer",
              value="owner",
              emoji="🛒",
          ),
          discord.SelectOption(
              label="Lawan transaksi (partner) sebagai Buyer",
              value="partner",
              emoji="💰",
          ),
      ],
  )
  async def select_buyer(
      self, interaction: discord.Interaction, select: discord.ui.Select
  ):
    if not self._is_ticket_owner(interaction):
      await interaction.response.send_message(
          "❌ Cuma pembuat tiket yang bisa pilih ini.", ephemeral=True
      )
      return

    self.buyer_choice = select.values[0]
    await interaction.response.defer()

  @discord.ui.button(
      label="Lanjut: Isi Nama Item & Harga",
      style=discord.ButtonStyle.green,
      row=2,
  )
  async def proceed(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if not self._is_ticket_owner(interaction):
      await interaction.response.send_message(
          "❌ Cuma pembuat tiket yang bisa lanjut.", ephemeral=True
      )
      return

    if self.partner is None:
      await interaction.response.send_message(
          "❌ Pilih dulu lawan transaksi kamu (dropdown pertama).", ephemeral=True
      )
      return
    if self.buyer_choice is None:
      await interaction.response.send_message(
          "❌ Pilih dulu siapa yang jadi Buyer (dropdown kedua).", ephemeral=True
      )
      return

    buyer = self.ticket_owner if self.buyer_choice == "owner" else self.partner
    seller = self.partner if self.buyer_choice == "owner" else self.ticket_owner

    # Modal cuma boleh dibuka sebagai respons LANGSUNG dari sebuah interaksi
    # (tombol/select), makanya urutannya: pilih partner & buyer -> baru modal.
    await interaction.response.send_modal(
        OrderModal(self.ticket_owner, self.partner, buyer, seller)
    )
    self.stop()


# ---------- Step 2: Modal nama item + harga (setelah partner & role dipilih) ----------
class OrderModal(discord.ui.Modal, title="XypherStore - Order Middleman"):

  nama_item = discord.ui.TextInput(
      label="Nama Item / Barang",
      placeholder="Contoh: Rayman / Wings / DL",
      max_length=100,
  )
  harga_barang = discord.ui.TextInput(
      label="Harga Barang (Angka saja)",
      placeholder="Contoh: 300000",
      max_length=20,
  )

  def __init__(
      self,
      ticket_owner: discord.Member,
      partner: discord.Member,
      buyer: discord.Member,
      seller: discord.Member,
  ):
    super().__init__()
    self.ticket_owner = ticket_owner
    self.partner = partner
    self.buyer = buyer
    self.seller = seller

  async def on_submit(self, interaction: discord.Interaction):
    raw = self.harga_barang.value.replace(".", "").replace(",", "").strip()
    if not raw.isdigit():
      await interaction.response.send_message(
          "❌ Harga barang harus berupa angka.", ephemeral=True
      )
      return

    await interaction.response.defer(ephemeral=True, thinking=True)
    await create_ticket_channel(
        interaction,
        self.ticket_owner,
        self.partner,
        self.buyer,
        self.seller,
        self.nama_item.value,
        int(raw),
    )


# ---------- Bikin channel private + kirim prompt Set Tax ----------
async def create_ticket_channel(
    interaction: discord.Interaction,
    owner: discord.Member,
    partner: discord.Member,
    buyer: discord.Member,
    seller: discord.Member,
    item_name: str,
    harga: int,
):
  guild = interaction.guild
  channel_name = f"mm-{owner.name.lower()}"

  existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
  if existing_channel:
    await interaction.followup.send(
        f"⚠️ Kamu sudah memiliki tiket aktif di {existing_channel.mention}!",
        ephemeral=True,
    )
    return

  overwrites = {
      guild.default_role: discord.PermissionOverwrite(read_messages=False),
      owner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
      partner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
      guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
  }

  category = discord.utils.get(guild.categories, name="🔒 _ ACTIVE TICKETS 🎫")
  if not category:
    category = discord.utils.get(guild.categories, name="_ ACTIVE TICKETS")

  ticket_channel = await guild.create_text_channel(
      channel_name, overwrites=overwrites, category=category
  )

  # fee & total belum dihitung -- nunggu Middleman input tax dulu lewat
  # tombol "Set Tax" di bawah.
  TICKET_DATA[ticket_channel.id] = {
      "owner_id": owner.id,
      "partner_id": partner.id,
      "buyer_id": buyer.id,
      "seller_id": seller.id,
      "item_name": item_name,
      "harga": harga,
      "tax_percent": None,
      "fee": None,
      "total": None,
      "world": None,
      "growid": None,
      "payment_confirmed": False,
      "seller_detail_filled": False,
      "buyer_growid_filled": False,
      "qris_message_id": None,
      "confirm_message_id": None,
      "seller_detail_message_id": None,
      "tax_prompt_message_id": None,
      # ---- Data buat alur setelah item di-drop ke Buyer ----
      "item_received": False,
      "item_received_message_id": None,
      "payout_method": None,
      "payout_account": None,
      "payout_name": None,
      "payout_qris_image_url": None,
      "payout_admin_fee": None,
      "payout_net": None,
      "payout_prompt_message_id": None,
      "payout_confirmed_mm": False,
      "mm_confirmed_by_id": None,
      "mm_confirmed_by_name": None,
      "mm_transfer_message_id": None,
      "payout_confirmed_seller": False,
      "seller_received_message_id": None,
  }

  intro_embed = buat_embed_dasar(
      guild,
      title="✨ Sesi Middleman XypherStore Dibuka",
      description=(
          f"Halo {owner.mention} & {partner.mention}! Terima kasih telah "
          "menggunakan jasa Middleman.\n"
          "───────────────────────\n"
          f"🛒 **Buyer:** {buyer.mention}\n"
          f"💰 **Seller:** {seller.mention}\n"
          f"📦 **Item:** {item_name}\n"
          f"💵 **Harga Barang:** Rp {harga:,}"
      ),
      color=discord.Color.green(),
  )
  intro_embed.set_footer(text="XypherStore Secure Transaction System")
  await ticket_channel.send(
      content=f"{owner.mention} {partner.mention}", embed=intro_embed
  )

  # Tombol tutup tiket tetap ready dari awal, gak perlu nunggu tax
  await ticket_channel.send(view=CloseTicketView())

  # ---------- Prompt buat Middleman input tax dulu ----------
  tax_prompt_embed = buat_embed_dasar(
      guild,
      title="🧮 Menunggu Middleman Set Tax",
      description=(
          f"Role `{MIDDLEMAN_ROLE_NAME}`, silakan klik tombol di bawah untuk "
          "menentukan persentase tax/fee middleman transaksi ini.\n\n"
          "Setelah tax diisi, bot akan otomatis menghitung total transfer dan "
          "menampilkan embed pembayaran + QRIS."
      ),
      color=discord.Color.orange(),
  )
  tax_prompt_msg = await ticket_channel.send(
      embed=tax_prompt_embed, view=TaxPromptView()
  )
  TICKET_DATA[ticket_channel.id]["tax_prompt_message_id"] = tax_prompt_msg.id

  await interaction.followup.send(
      f"✅ Berhasil! Cek channel transaksi kamu di: {ticket_channel.mention}",
      ephemeral=True,
  )


# ---------- Tombol "Set Tax" -- KHUSUS role Middleman ----------
class TaxPromptView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="⚙️ Set Tax (Middleman)",
      style=discord.ButtonStyle.blurple,
      custom_id="set_tax_btn",
  )
  async def set_tax(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if not is_middleman(interaction.user):
      await interaction.response.send_message(
          f"❌ Cuma role `{MIDDLEMAN_ROLE_NAME}` yang bisa set tax transaksi ini.",
          ephemeral=True,
      )
      return

    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if data.get("tax_percent") is not None:
      await interaction.response.send_message(
          "⚠️ Tax transaksi ini sudah pernah diisi.", ephemeral=True
      )
      return

    await interaction.response.send_modal(TaxModal())


class TaxModal(discord.ui.Modal, title="Set Tax Middleman"):

  tax_persen = discord.ui.TextInput(
      label="Persentase Tax (%)",
      placeholder="Contoh: 3 atau 2.5",
      default=str(MM_FEE_PERCENT),
      max_length=10,
  )

  async def on_submit(self, interaction: discord.Interaction):
    raw = self.tax_persen.value.strip().replace(",", ".")
    try:
      persen = float(raw)
    except ValueError:
      await interaction.response.send_message(
          "❌ Tax harus berupa angka, contoh: 3 atau 2.5", ephemeral=True
      )
      return

    if persen < 0 or persen > 100:
      await interaction.response.send_message(
          "❌ Tax harus di antara 0 - 100.", ephemeral=True
      )
      return

    channel_id = interaction.channel.id
    data = TICKET_DATA.get(channel_id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    fee, total = hitung_fee(data["harga"], persen)
    data["tax_percent"] = persen
    data["fee"] = fee
    data["total"] = total

    await interaction.response.defer()

    # Matikan tombol Set Tax biar gak dobel-klik
    prompt_msg_id = data.get("tax_prompt_message_id")
    if prompt_msg_id:
      try:
        prompt_msg = await interaction.channel.fetch_message(prompt_msg_id)
        disabled_view = TaxPromptView()
        for child in disabled_view.children:
          child.disabled = True
        await prompt_msg.edit(view=disabled_view)
      except discord.NotFound:
        pass

    await kirim_payment_embed(interaction.channel, data, persen)


# ---------- Kirim embed "Pembayaran ke XypherStore Middleman" + QRIS ----------
async def kirim_payment_embed(
    channel: discord.TextChannel, data: dict, persen: float
):
  guild = channel.guild
  buyer = guild.get_member(data["buyer_id"])
  seller = guild.get_member(data["seller_id"])
  buyer_mention = buyer.mention if buyer else "Buyer"
  seller_mention = seller.mention if seller else "Seller"

  payment_embed = buat_embed_dasar(
      guild,
      title="💳 Pembayaran ke XypherStore Middleman",
      color=discord.Color.blue(),
  )
  payment_embed.set_footer(text="XypherStore Secure Transaction System")
  payment_embed.add_field(
      name="🔹 Detail Transaksi",
      value=(
          f"**Item / Barang:** {data['item_name']}\n"
          f"**Harga Barang:** Rp {data['harga']:,}\n"
          f"**Fee Middleman ({persen:g}%):** Rp {data['fee']:,}\n"
          f"**Total Transfer:** Rp {data['total']:,}"
      ),
      inline=False,
  )
  payment_embed.add_field(
      name="📋 Alur Transaksi",
      value=(
          f"1️⃣ {buyer_mention} (**Buyer**) wajib transfer **Total Transfer** di atas "
          "ke rekening/e-wallet MM resmi (scan QRIS di bawah). Fee middleman sudah "
          "termasuk di nominal ini, Seller tidak perlu transfer apa-apa.\n"
          f"2️⃣ Setelah dana masuk & diamankan oleh MM, MM akan memberi instruksi ke "
          f"{seller_mention} (**Seller**) untuk menyerahkan item ke Buyer.\n"
          "3️⃣ Buyer mengecek item di dalam game & mengonfirmasi ke MM jika item sudah diterima dengan aman.\n"
          "4️⃣ Setelah fix, MM akan meneruskan dana bersih (harga barang) ke pihak Seller."
      ),
      inline=False,
  )
  payment_embed.add_field(
      name="⚠️ Catatan",
      value=(
          "• Jangan melakukan transaksi di luar jalur instruksi MM.\n"
          "• Segala bentuk penipuan di luar arahan MM di luar tanggung jawab MM."
      ),
      inline=False,
  )

  tax_channel = cari_channel(guild, TAX_CHANNEL_NAME)
  tax_channel_ref = tax_channel.mention if tax_channel else f"#{TAX_CHANNEL_NAME}"
  payment_embed.add_field(
      name="🧮 Cek Ulang Perhitungan",
      value=(
          f"Ragu sama hitungan fee di atas? Cek manual di {tax_channel_ref} "
          "atau ketik `/mm` di mana aja buat hitung sendiri (pakai tax default)."
      ),
      inline=False,
  )

  files = []
  if os.path.exists(QRIS_IMAGE_PATH):
    file = discord.File(QRIS_IMAGE_PATH, filename="qris.png")
    payment_embed.set_image(url="attachment://qris.png")
    files.append(file)
  else:
    payment_embed.add_field(
        name="⚠️ QRIS belum di-setup", value="Admin belum upload gambar QRIS.", inline=False
    )

  qris_message = await channel.send(
      embed=payment_embed, files=files, view=ConfirmPaymentView()
  )
  data["qris_message_id"] = qris_message.id


# ---------- Tombol Konfirmasi Pembayaran (khusus role Middleman) ----------
# Klik tombol ini TIDAK langsung buka modal -- cuma kasih pesan konfirmasi
# bahwa dana sudah diterima. Detail item/world (Seller) dan GrowID (Buyer)
# dipisah jadi 2 modal berbeda, masing-masing cuma bisa diisi oleh pihak yang
# bersangkutan.
class ConfirmPaymentView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="✅ Konfirmasi Pembayaran",
      style=discord.ButtonStyle.blurple,
      custom_id="confirm_payment_btn",
  )
  async def confirm_payment(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if not is_middleman(interaction.user):
      await interaction.response.send_message(
          f"❌ Cuma role `{MIDDLEMAN_ROLE_NAME}` yang bisa konfirmasi pembayaran.",
          ephemeral=True,
      )
      return

    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return
    if data.get("payment_confirmed"):
      await interaction.response.send_message(
          "⚠️ Pembayaran untuk tiket ini sudah dikonfirmasi sebelumnya.",
          ephemeral=True,
      )
      return

    data["payment_confirmed"] = True

    # Matikan tombol Konfirmasi Pembayaran biar gak dobel-klik
    disabled_view = ConfirmPaymentView()
    for child in disabled_view.children:
      child.disabled = True
    await interaction.response.edit_message(view=disabled_view)

    seller = interaction.guild.get_member(data["seller_id"])
    confirm_embed = discord.Embed(
        title="✅ Pembayaran Terkonfirmasi",
        description=(
            f"Dana sudah diterima & diamankan oleh {interaction.user.mention}.\n\n"
            f"{seller.mention if seller else 'Seller'}, silakan klik tombol di bawah "
            "untuk isi Nama Item & World tempat transaksi."
        ),
        color=discord.Color.gold(),
    )
    confirm_msg = await interaction.channel.send(
        embed=confirm_embed, view=SellerDetailView()
    )
    data["confirm_message_id"] = confirm_msg.id


# ---------- Tombol "Isi Detail Transaksi" -- KHUSUS SELLER (item + world) ----------
class SellerDetailView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="📝 Isi Item & World (Seller)",
      style=discord.ButtonStyle.green,
      custom_id="seller_detail_btn",
  )
  async def fill_detail(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if interaction.user.id != data["seller_id"]:
      await interaction.response.send_message(
          "❌ Cuma Seller di transaksi ini yang bisa isi bagian ini.",
          ephemeral=True,
      )
      return

    if data.get("seller_detail_filled"):
      await interaction.response.send_message(
          "⚠️ Detail item & world sudah diisi sebelumnya.", ephemeral=True
      )
      return

    await interaction.response.send_modal(SellerDetailModal(data.get("item_name", "")))


class SellerDetailModal(discord.ui.Modal, title="Detail Item & World (Seller)"):

  nama_item = discord.ui.TextInput(label="Nama Item / Barang", max_length=100)
  world = discord.ui.TextInput(
      label="Nama World Growtopia",
      placeholder="Contoh: XYPHERMM",
      max_length=50,
  )

  def __init__(self, default_item_name: str = ""):
    super().__init__()
    if default_item_name:
      self.nama_item.default = default_item_name

  async def on_submit(self, interaction: discord.Interaction):
    channel_id = interaction.channel.id
    data = TICKET_DATA.get(channel_id, {})
    data["item_name"] = self.nama_item.value
    data["world"] = self.world.value
    data["seller_detail_filled"] = True
    TICKET_DATA[channel_id] = data

    buyer = interaction.guild.get_member(data["buyer_id"])

    result_embed = discord.Embed(
        title="🌍 World Transaksi Sudah Diset Seller",
        color=discord.Color.gold(),
    )
    result_embed.add_field(name="📦 Item", value=self.nama_item.value, inline=True)
    result_embed.add_field(
        name="💰 Harga", value=f"Rp {data.get('harga', 0):,}", inline=True
    )
    result_embed.add_field(name="🌍 World", value=self.world.value, inline=False)
    result_embed.set_footer(text="Selanjutnya Buyer wajib isi GrowID di bawah.")

    await interaction.response.send_message(
        content=f"{buyer.mention if buyer else ''}", embed=result_embed
    )

    # Matikan tombol seller biar gak dobel-isi
    seller_msg_id = data.get("confirm_message_id")
    if seller_msg_id:
      try:
        seller_msg = await interaction.channel.fetch_message(seller_msg_id)
        disabled_view = SellerDetailView()
        for child in disabled_view.children:
          child.disabled = True
        await seller_msg.edit(view=disabled_view)
      except discord.NotFound:
        pass

    # Munculin tombol buat Buyer isi GrowID sendiri
    growid_prompt = await interaction.channel.send(
        content=f"{buyer.mention if buyer else ''}",
        embed=discord.Embed(
            description="🔑 Buyer, silakan klik tombol di bawah untuk isi GrowID kamu "
            "(akun yang bakal nerima item), biar Seller gak salah kasih ke orang lain.",
            color=discord.Color.blue(),
        ),
        view=BuyerGrowIDView(),
    )
    data["seller_detail_message_id"] = growid_prompt.id


# ---------- Tombol "Isi GrowID" -- KHUSUS BUYER ----------
class BuyerGrowIDView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="🔑 Isi GrowID Saya (Buyer)",
      style=discord.ButtonStyle.green,
      custom_id="buyer_growid_btn",
  )
  async def fill_growid(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if interaction.user.id != data["buyer_id"]:
      await interaction.response.send_message(
          "❌ Cuma Buyer di transaksi ini yang bisa isi GrowID.", ephemeral=True
      )
      return

    if data.get("buyer_growid_filled"):
      await interaction.response.send_message(
          "⚠️ GrowID sudah diisi sebelumnya.", ephemeral=True
      )
      return

    await interaction.response.send_modal(BuyerGrowIDModal())


class BuyerGrowIDModal(discord.ui.Modal, title="GrowID Kamu (Buyer)"):

  growid = discord.ui.TextInput(
      label="GrowID Penerima Item",
      placeholder="Contoh: XYPHERSTORE",
      max_length=50,
  )

  async def on_submit(self, interaction: discord.Interaction):
    channel_id = interaction.channel.id
    data = TICKET_DATA.get(channel_id, {})
    data["growid"] = self.growid.value
    data["buyer_growid_filled"] = True
    TICKET_DATA[channel_id] = data

    seller = interaction.guild.get_member(data["seller_id"])

    final_embed = buat_embed_dasar(
        interaction.guild,
        title="✅ Semua Detail Transaksi Lengkap",
        color=discord.Color.gold(),
    )
    final_embed.add_field(name="📦 Item", value=data.get("item_name", "-"), inline=True)
    final_embed.add_field(
        name="💰 Harga", value=f"Rp {data.get('harga', 0):,}", inline=True
    )
    final_embed.add_field(name="🌍 World", value=data.get("world", "-"), inline=False)
    final_embed.add_field(
        name="🧑‍🌾 GrowID Penerima (Buyer)", value=self.growid.value, inline=False
    )
    final_embed.set_footer(
        text=f"{seller.name if seller else 'Seller'}, silakan drop item ke GrowID di atas, di world yang sudah ditentukan."
    )

    await interaction.response.send_message(
        content=f"{seller.mention if seller else ''}", embed=final_embed
    )

    # Matikan tombol isi GrowID biar gak dobel-isi
    msg_id = data.get("seller_detail_message_id")
    if msg_id:
      try:
        msg = await interaction.channel.fetch_message(msg_id)
        disabled_view = BuyerGrowIDView()
        for child in disabled_view.children:
          child.disabled = True
        await msg.edit(view=disabled_view)
      except discord.NotFound:
        pass

    # Munculin tombol buat Buyer konfirmasi barang udah diterima di game
    item_received_prompt = await interaction.channel.send(
        content=f"{interaction.guild.get_member(data['buyer_id']).mention if interaction.guild.get_member(data['buyer_id']) else ''}",
        embed=discord.Embed(
            description="🎮 Buyer, silakan cek item-nya di dalam game dulu. Kalau sudah "
            "diterima dengan aman, klik tombol di bawah.",
            color=discord.Color.blue(),
        ),
        view=ItemReceivedView(),
    )
    data["item_received_message_id"] = item_received_prompt.id


# ---------- Tombol "Barang Sudah Diterima" -- KHUSUS BUYER ----------
class ItemReceivedView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="✅ Barang Sudah Diterima (Buyer)",
      style=discord.ButtonStyle.green,
      custom_id="item_received_btn",
  )
  async def item_received(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if interaction.user.id != data["buyer_id"]:
      await interaction.response.send_message(
          "❌ Cuma Buyer di transaksi ini yang bisa konfirmasi ini.", ephemeral=True
      )
      return

    if data.get("item_received"):
      await interaction.response.send_message(
          "⚠️ Konfirmasi ini sudah pernah dilakukan.", ephemeral=True
      )
      return

    data["item_received"] = True

    # Matikan tombol biar gak dobel-klik
    disabled_view = ItemReceivedView()
    for child in disabled_view.children:
      child.disabled = True
    await interaction.response.edit_message(view=disabled_view)

    seller = interaction.guild.get_member(data["seller_id"])
    payout_embed = buat_embed_dasar(
        interaction.guild,
        title="💰 Waktunya Pencairan Dana ke Seller",
        description=(
            f"Buyer sudah konfirmasi menerima barang dengan aman.\n\n"
            f"{seller.mention if seller else 'Seller'}, silakan pilih metode "
            "pencairan dana kamu di bawah ini."
        ),
        color=discord.Color.gold(),
    )
    payout_embed.add_field(
        name="ℹ️ Catatan Potongan Admin",
        value=(
            f"• DANA: potong admin Rp {PAYOUT_ADMIN_FEE['DANA']:,}\n"
            f"• GoPay: gratis admin\n"
            f"• QRIS: gratis admin"
        ),
        inline=False,
    )
    payout_msg = await interaction.channel.send(
        content=f"{seller.mention if seller else ''}",
        embed=payout_embed,
        view=PayoutMethodView(),
    )
    data["payout_prompt_message_id"] = payout_msg.id


# ---------- Dropdown "Pilih Metode Pencairan Dana" -- KHUSUS SELLER ----------
class PayoutMethodView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.select(
      placeholder="💳 Pilih metode pencairan dana kamu",
      min_values=1,
      max_values=1,
      custom_id="payout_method_select",
      options=[
          discord.SelectOption(label="DANA", value="DANA", emoji="🟠"),
          discord.SelectOption(label="GoPay", value="GoPay", emoji="🔵"),
          discord.SelectOption(label="QRIS", value="QRIS", emoji="🔲"),
      ],
  )
  async def select_method(
      self, interaction: discord.Interaction, select: discord.ui.Select
  ):
    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if interaction.user.id != data["seller_id"]:
      await interaction.response.send_message(
          "❌ Cuma Seller di transaksi ini yang bisa isi bagian ini.",
          ephemeral=True,
      )
      return

    if data.get("payout_method"):
      await interaction.response.send_message(
          "⚠️ Info pencairan dana sudah pernah diisi.", ephemeral=True
      )
      return

    metode = select.values[0]
    if metode == "QRIS":
      await interaction.response.send_modal(PayoutQrisModal())
    else:
      await interaction.response.send_modal(PayoutDetailModal(metode))


class PayoutDetailModal(discord.ui.Modal, title="Info Pencairan Dana (Seller)"):

  nomor_tujuan = discord.ui.TextInput(
      label="Nomor Tujuan (DANA/GoPay)",
      placeholder="Contoh: 0812-3456-7890",
      max_length=30,
  )
  nama_penerima = discord.ui.TextInput(
      label="Nama Penerima",
      placeholder="Contoh: Xypher",
      max_length=50,
  )

  def __init__(self, metode: str):
    super().__init__()
    self.metode = metode

  async def on_submit(self, interaction: discord.Interaction):
    channel_id = interaction.channel.id
    data = TICKET_DATA.get(channel_id, {})

    fee = PAYOUT_ADMIN_FEE.get(self.metode, 0)
    net = data["harga"] - fee

    data["payout_method"] = self.metode
    data["payout_account"] = self.nomor_tujuan.value
    data["payout_name"] = self.nama_penerima.value
    data["payout_admin_fee"] = fee
    data["payout_net"] = net
    TICKET_DATA[channel_id] = data

    # Matikan dropdown biar gak dobel-isi
    await _matikan_payout_dropdown(interaction.channel, data)

    mm_embed = buat_embed_dasar(
        interaction.guild,
        title="🏦 Instruksi Transfer ke Seller",
        description=f"Role `{MIDDLEMAN_ROLE_NAME}`, silakan transfer sesuai detail di bawah.",
        color=discord.Color.orange(),
    )
    mm_embed.add_field(name="💳 Metode", value=self.metode, inline=True)
    mm_embed.add_field(name="🔢 Nomor Tujuan", value=self.nomor_tujuan.value, inline=True)
    mm_embed.add_field(name="🙍 Nama Penerima", value=self.nama_penerima.value, inline=False)
    mm_embed.add_field(
        name="🧮 Rincian",
        value=(
            f"**Harga Barang:** Rp {data['harga']:,}\n"
            f"**Potongan Admin ({self.metode}):** Rp {fee:,}\n"
            f"**Total Transfer ke Seller:** Rp {net:,}"
        ),
        inline=False,
    )

    await interaction.response.send_message(embed=mm_embed, view=PayoutConfirmMMView())
    sent = await interaction.original_response()
    data["mm_transfer_message_id"] = sent.id


class PayoutQrisModal(discord.ui.Modal, title="Info Pencairan Dana - QRIS (Seller)"):

  nama_penerima = discord.ui.TextInput(
      label="Nama Pemilik QRIS",
      placeholder="Contoh: Xypher",
      max_length=50,
  )

  async def on_submit(self, interaction: discord.Interaction):
    channel_id = interaction.channel.id
    data = TICKET_DATA.get(channel_id, {})

    # QRIS gratis admin -- gak ada nomor tujuan, yang dibutuhin gambar QRIS-nya.
    fee = PAYOUT_ADMIN_FEE.get("QRIS", 0)
    net = data["harga"] - fee

    data["payout_name"] = self.nama_penerima.value
    data["payout_admin_fee"] = fee
    data["payout_net"] = net
    TICKET_DATA[channel_id] = data

    await interaction.response.send_message(
        content=f"{interaction.user.mention}",
        embed=discord.Embed(
            description=(
                "📸 Silakan **kirim/upload gambar QRIS kamu** di channel ini sekarang "
                "(kirim sebagai lampiran gambar, bukan link).\n\n"
                "Kamu punya waktu 5 menit sebelum diminta ulang."
            ),
            color=discord.Color.blue(),
        ),
    )

    def cek_pesan(m: discord.Message) -> bool:
      return (
          m.channel.id == channel_id
          and m.author.id == data["seller_id"]
          and len(m.attachments) > 0
      )

    try:
      pesan_qris = await bot.wait_for("message", check=cek_pesan, timeout=300)
    except asyncio.TimeoutError:
      await interaction.channel.send(
          f"⚠️ {interaction.user.mention} waktu upload QRIS habis. Silakan pilih "
          "ulang metode QRIS di dropdown untuk coba lagi."
      )
      return

    gambar_qris_url = pesan_qris.attachments[0].url
    data["payout_method"] = "QRIS"
    data["payout_qris_image_url"] = gambar_qris_url

    # Matikan dropdown biar gak dobel-isi, baru sekarang setelah gambar diterima
    await _matikan_payout_dropdown(interaction.channel, data)

    mm_embed = buat_embed_dasar(
        interaction.guild,
        title="🏦 Instruksi Transfer ke Seller (QRIS)",
        description=(
            f"Role `{MIDDLEMAN_ROLE_NAME}`, silakan scan QRIS di bawah untuk transfer."
        ),
        color=discord.Color.orange(),
    )
    mm_embed.add_field(name="💳 Metode", value="QRIS", inline=True)
    mm_embed.add_field(name="🙍 Nama Pemilik", value=self.nama_penerima.value, inline=True)
    mm_embed.add_field(
        name="🧮 Rincian",
        value=(
            f"**Harga Barang:** Rp {data['harga']:,}\n"
            f"**Potongan Admin (QRIS):** Rp {fee:,}\n"
            f"**Total Transfer ke Seller:** Rp {net:,}"
        ),
        inline=False,
    )
    mm_embed.set_image(url=gambar_qris_url)

    await interaction.channel.send(embed=mm_embed, view=PayoutConfirmMMView())


async def _matikan_payout_dropdown(channel: discord.TextChannel, data: dict):
  """Matikan dropdown pilih metode pencairan dana biar gak dobel-isi."""
  prompt_id = data.get("payout_prompt_message_id")
  if prompt_id:
    try:
      prompt_msg = await channel.fetch_message(prompt_id)
      disabled_view = PayoutMethodView()
      for child in disabled_view.children:
        child.disabled = True
      await prompt_msg.edit(view=disabled_view)
    except discord.NotFound:
      pass


# ---------- Tombol "Sudah Transfer ke Seller" -- KHUSUS role Middleman ----------
class PayoutConfirmMMView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="✅ Sudah Transfer ke Seller",
      style=discord.ButtonStyle.blurple,
      custom_id="mm_transfer_confirm_btn",
  )
  async def confirm_transfer(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if not is_middleman(interaction.user):
      await interaction.response.send_message(
          f"❌ Cuma role `{MIDDLEMAN_ROLE_NAME}` yang bisa konfirmasi ini.",
          ephemeral=True,
      )
      return

    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if data.get("payout_confirmed_mm"):
      await interaction.response.send_message(
          "⚠️ Transfer ke Seller sudah pernah dikonfirmasi.", ephemeral=True
      )
      return

    data["payout_confirmed_mm"] = True
    data["mm_confirmed_by_id"] = interaction.user.id
    data["mm_confirmed_by_name"] = interaction.user.display_name

    disabled_view = PayoutConfirmMMView()
    for child in disabled_view.children:
      child.disabled = True
    await interaction.response.edit_message(view=disabled_view)

    seller = interaction.guild.get_member(data["seller_id"])
    seller_confirm_embed = discord.Embed(
        title="🔔 Cek Dana Kamu",
        description=(
            f"{interaction.user.mention} sudah menandai dana sebesar "
            f"Rp {data['payout_net']:,} sudah ditransfer ke kamu lewat "
            f"{data['payout_method']}.\n\n"
            f"{seller.mention if seller else 'Seller'}, tolong cek dan klik "
            "tombol di bawah kalau dana sudah benar-benar masuk."
        ),
        color=discord.Color.blue(),
    )
    seller_msg = await interaction.channel.send(
        content=f"{seller.mention if seller else ''}",
        embed=seller_confirm_embed,
        view=SellerReceivedView(),
    )
    data["seller_received_message_id"] = seller_msg.id


# ---------- Tombol "Dana Diterima" -- KHUSUS SELLER, lalu auto-post testimoni ----------
class SellerReceivedView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="✅ Dana Diterima",
      style=discord.ButtonStyle.green,
      custom_id="seller_received_btn",
  )
  async def confirm_received(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    data = TICKET_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data tiket tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if interaction.user.id != data["seller_id"]:
      await interaction.response.send_message(
          "❌ Cuma Seller di transaksi ini yang bisa konfirmasi ini.", ephemeral=True
      )
      return

    if data.get("payout_confirmed_seller"):
      await interaction.response.send_message(
          "⚠️ Konfirmasi ini sudah pernah dilakukan.", ephemeral=True
      )
      return

    data["payout_confirmed_seller"] = True

    disabled_view = SellerReceivedView()
    for child in disabled_view.children:
      child.disabled = True
    await interaction.response.edit_message(view=disabled_view)

    await interaction.channel.send(
        embed=discord.Embed(
            title="🎉 Transaksi Selesai!",
            description=(
                "Semua tahap sudah dikonfirmasi kedua belah pihak. Terima kasih "
                "sudah pakai jasa Middleman XypherStore! Tiket ini bisa ditutup "
                "kapan saja lewat tombol 'Tutup Tiket'."
            ),
            color=discord.Color.green(),
        )
    )

    await kirim_testimoni(interaction.channel, data)


# ---------- Generator gambar kartu proof/testimoni (pakai Pillow) ----------
_KARTU_BG = (18, 20, 26)
_KARTU_CARD = (30, 33, 41)
_KARTU_GOLD = (245, 197, 66)
_KARTU_GREEN = (67, 181, 129)
_KARTU_TEXT = (255, 255, 255)
_KARTU_MUTED = (148, 155, 168)
_KARTU_W, _KARTU_H = 1000, 560


def _teks_aman(teks: str, fallback: str = "-") -> str:
  """Buang karakter unicode 'fancy'/emoji yang gak punya glyph di font Poppins
  (misal nickname yang pakai gaya font aneh-aneh), biar gak muncul kotak tofu
  di kartu. Huruf/angka/tanda baca biasa (termasuk aksen umum) tetap aman.
  Kalau hasil akhirnya gak ada huruf/angka sama sekali (nickname-nya fancy
  semua), pakai fallback daripada nyisain simbol doang."""
  if not teks:
    return fallback
  hasil = "".join(c for c in teks if ord(c) < 0x2000)
  hasil = " ".join(hasil.split())
  if not hasil or not any(c.isalnum() for c in hasil):
    return fallback
  return hasil


def _font(nama_file: str, size: int) -> ImageFont.FreeTypeFont:
  try:
    return ImageFont.truetype(f"{FONT_DIR}/{nama_file}", size)
  except OSError:
    # Fallback ke font default Pillow kalau file font gak ketemu di server
    return ImageFont.load_default(size=size)


def _circle_crop(img: Image.Image, size: int) -> Image.Image:
  img = ImageOps.fit(img.convert("RGBA"), (size, size), Image.LANCZOS)
  mask = Image.new("L", (size, size), 0)
  ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
  out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
  out.paste(img, (0, 0), mask)
  return out


def _dashed_line(draw: ImageDraw.ImageDraw, x1, y, x2, color, dash=10, gap=8, width=2):
  x = x1
  while x < x2:
    draw.line([(x, y), (min(x + dash, x2), y)], fill=color, width=width)
    x += dash + gap


def _buat_kartu_proof_mm(
    item_name: str,
    nominal: int,
    buyer_name: str,
    seller_name: str,
    mm_name: str,
    ticket_label: str,
    timestamp_str: str,
    logo_bytes: bytes = None,
    buyer_avatar_bytes: bytes = None,
    seller_avatar_bytes: bytes = None,
    mm_avatar_bytes: bytes = None,
) -> io.BytesIO:
  """Bikin gambar kartu proof transaksi Middleman (bentuk tiket, 3 pihak:
  Buyer/Seller/Middleman) & kembalikan sebagai BytesIO PNG, siap dikirim
  lewat discord.File."""
  item_name = _teks_aman(item_name, "-")
  buyer_name = _teks_aman(buyer_name, "Buyer")
  seller_name = _teks_aman(seller_name, "Seller")
  mm_name = _teks_aman(mm_name, "Middleman")

  base = Image.new("RGB", (_KARTU_W, _KARTU_H), _KARTU_BG)
  draw = ImageDraw.Draw(base)

  card_box = (40, 40, _KARTU_W - 40, _KARTU_H - 40)
  card_layer = Image.new("RGBA", (_KARTU_W, _KARTU_H), (0, 0, 0, 0))
  ImageDraw.Draw(card_layer).rounded_rectangle(card_box, radius=28, fill=_KARTU_CARD + (255,))
  base.paste(card_layer, (0, 0), card_layer)
  draw = ImageDraw.Draw(base)

  # Notch ala tiket di sisi kiri & kanan
  notch_y = 40 + int((_KARTU_H - 80) * 0.42)
  r_notch = 22
  draw.ellipse((card_box[0] - r_notch, notch_y - r_notch, card_box[0] + r_notch, notch_y + r_notch), fill=_KARTU_BG)
  draw.ellipse((card_box[2] - r_notch, notch_y - r_notch, card_box[2] + r_notch, notch_y + r_notch), fill=_KARTU_BG)

  # Header: logo toko + judul + badge "SELESAI"
  header_y = 78
  if logo_bytes:
    try:
      logo_img = Image.open(io.BytesIO(logo_bytes))
      logo = _circle_crop(logo_img, 72)
      base.paste(logo, (74, header_y), logo)
      text_x = 74 + 72 + 22
    except Exception:
      text_x = 74
  else:
    text_x = 74

  f_brand = _font("Poppins-Bold.ttf", 30)
  f_sub = _font("Poppins-Regular.ttf", 17)
  draw.text((text_x, header_y + 4), "XypherStore", font=f_brand, fill=_KARTU_TEXT)
  draw.text((text_x, header_y + 42), "Bukti Transaksi Middleman", font=f_sub, fill=_KARTU_MUTED)

  f_badge = _font("Poppins-SemiBold.ttf", 18)
  badge_text = "SELESAI"
  icon_d = 20
  text_w = draw.textlength(badge_text, font=f_badge)
  badge_w = 20 + icon_d + 10 + text_w + 20
  badge_h = 42
  badge_box = (_KARTU_W - 40 - badge_w - 20, header_y + 8, _KARTU_W - 40 - 20, header_y + 8 + badge_h)
  draw.rounded_rectangle(badge_box, radius=badge_h // 2, fill=_KARTU_GREEN)
  icon_cx = badge_box[0] + 20 + icon_d / 2
  icon_cy = (badge_box[1] + badge_box[3]) / 2
  check_color = (15, 20, 15)
  draw.line(
      [(icon_cx - 6, icon_cy), (icon_cx - 2, icon_cy + 5), (icon_cx + 7, icon_cy - 6)],
      fill=check_color, width=3, joint="curve",
  )
  draw.text((icon_cx + icon_d / 2 + 10, badge_box[1] + 9), badge_text, font=f_badge, fill=check_color)

  # Garis putus-putus pemisah header
  _dashed_line(draw, card_box[0] + 46, notch_y, card_box[2] - 46, (60, 64, 74))

  # Body: Item / Nominal (cuma 2 kolom -- gak ada konsep jumlah di transaksi MM)
  body_y = notch_y + 42
  f_label = _font("Poppins-Regular.ttf", 16)
  f_value = _font("Poppins-SemiBold.ttf", 26)
  col_w = (card_box[2] - card_box[0] - 92) / 2
  for i, (label, value, color) in enumerate([
      ("ITEM", item_name, _KARTU_TEXT),
      ("NOMINAL", f"Rp {nominal:,}", _KARTU_GOLD),
  ]):
    x = card_box[0] + 46 + i * col_w
    draw.text((x, body_y), label, font=f_label, fill=_KARTU_MUTED)
    draw.text((x, body_y + 26), value, font=f_value, fill=color)

  # Garis putus-putus sebelum footer
  sep2_y = body_y + 100
  _dashed_line(draw, card_box[0] + 46, sep2_y, card_box[2] - 46, (60, 64, 74))

  # Buyer, Seller & Middleman (avatar + nama) -- 3 kolom
  row_y = sep2_y + 30
  avatar_size = 52
  f_role = _font("Poppins-Regular.ttf", 14)
  f_name = _font("Poppins-SemiBold.ttf", 18)
  col3_w = (card_box[2] - card_box[0] - 92) / 3

  def _draw_person(x, role, name, avatar_bytes):
    name_x = x
    if avatar_bytes:
      try:
        av_img = Image.open(io.BytesIO(avatar_bytes))
        av = _circle_crop(av_img, avatar_size)
        base.paste(av, (int(x), row_y), av)
        name_x = x + avatar_size + 14
      except Exception:
        pass
    draw.text((name_x, row_y + 2), role, font=f_role, fill=_KARTU_MUTED)
    draw.text((name_x, row_y + 20), name, font=f_name, fill=_KARTU_TEXT)

  _draw_person(card_box[0] + 46, "BUYER", buyer_name, buyer_avatar_bytes)
  _draw_person(card_box[0] + 46 + col3_w, "SELLER", seller_name, seller_avatar_bytes)
  _draw_person(card_box[0] + 46 + col3_w * 2, "MIDDLEMAN", mm_name, mm_avatar_bytes)

  # Footer
  f_footer = _font("Poppins-Regular.ttf", 14)
  draw.text(
      (card_box[0] + 46, card_box[3] - 40),
      f"{ticket_label}  •  {timestamp_str}",
      font=f_footer,
      fill=_KARTU_MUTED,
  )

  buffer = io.BytesIO()
  base.save(buffer, format="PNG")
  buffer.seek(0)
  return buffer


async def _baca_avatar_bytes(member: discord.Member | None) -> bytes | None:
  if member is None:
    return None
  try:
    return await member.display_avatar.read()
  except Exception:
    return None


# ---------- Kirim proof/testimoni otomatis ke channel #testimoni ----------
async def kirim_testimoni(channel: discord.TextChannel, data: dict):
  guild = channel.guild
  buyer = guild.get_member(data["buyer_id"])
  seller = guild.get_member(data["seller_id"])
  mm_member = None
  if data.get("mm_confirmed_by_id"):
    mm_member = guild.get_member(data["mm_confirmed_by_id"])

  logo_bytes = None
  if guild.icon:
    try:
      logo_bytes = await guild.icon.read()
    except Exception:
      logo_bytes = None

  buyer_avatar_bytes = await _baca_avatar_bytes(buyer)
  seller_avatar_bytes = await _baca_avatar_bytes(seller)
  mm_avatar_bytes = await _baca_avatar_bytes(mm_member)

  waktu_sekarang = discord.utils.utcnow().strftime("%d %b %Y, %H:%M UTC")

  buffer = _buat_kartu_proof_mm(
      item_name=data.get("item_name", "-"),
      nominal=data.get("harga", 0),
      buyer_name=buyer.display_name if buyer else "Buyer",
      seller_name=seller.display_name if seller else "Seller",
      mm_name=data.get("mm_confirmed_by_name", "Middleman"),
      ticket_label=f"Ticket: #{channel.name}",
      timestamp_str=waktu_sekarang,
      logo_bytes=logo_bytes,
      buyer_avatar_bytes=buyer_avatar_bytes,
      seller_avatar_bytes=seller_avatar_bytes,
      mm_avatar_bytes=mm_avatar_bytes,
  )
  proof_file = discord.File(buffer, filename="proof_transaksi.png")

  testimoni_channel = cari_channel(guild, TESTIMONI_CHANNEL_NAME)
  target_channel = testimoni_channel if testimoni_channel else channel
  if not testimoni_channel:
    await channel.send(
        f"⚠️ Channel `#{TESTIMONI_CHANNEL_NAME}` belum ada, proof dikirim di sini dulu:"
    )
  await target_channel.send(file=proof_file)


# ---------- Tombol Tutup Tiket ----------
class CloseTicketView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="🔒 Tutup Tiket",
      style=discord.ButtonStyle.red,
      custom_id="close_ticket_btn",
  )
  async def close_ticket(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_message(
        "⚠️ Tiket akan ditutup dan channel ini akan dihapus dalam 3 detik...",
        ephemeral=False,
    )
    TICKET_DATA.pop(interaction.channel.id, None)
    await asyncio.sleep(3)
    await interaction.channel.delete()


# ---------- Tombol Utama Pembuat Tiket ----------
class TicketButton(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="🛒 Order Middleman (Buat Tiket)",
      style=discord.ButtonStyle.green,
      custom_id="create_ticket_btn",
  )
  async def create_ticket(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_message(
        "👥 Pilih lawan transaksi kamu & tentukan siapa Buyer di bawah ini.\n\n"
        "⚠️ **Pastikan lawan transaksi sudah join server ini dulu**, "
        "biar bisa muncul di list & langsung diundang ke channel.",
        view=SetupView(interaction.user),
        ephemeral=True,
    )


@bot.event
async def on_ready():
  print(f"Bot {bot.user.name} berhasil online dan siap bertugas!")
  bot.add_view(TicketButton())
  bot.add_view(CloseTicketView())
  bot.add_view(TaxPromptView())
  bot.add_view(ConfirmPaymentView())
  bot.add_view(SellerDetailView())
  bot.add_view(BuyerGrowIDView())
  bot.add_view(ItemReceivedView())
  bot.add_view(PayoutMethodView())
  bot.add_view(PayoutConfirmMMView())
  bot.add_view(SellerReceivedView())


@bot.tree.command(
    name="mm",
    description="Hitung fee middleman & total transfer dari harga barang (pakai tax default)",
)
@app_commands.describe(
    harga="Harga barang, angka saja (contoh: 50000)",
    persen="Opsional: persentase tax custom (default pakai MM_FEE_PERCENT)",
)
async def mm_calculator(
    interaction: discord.Interaction, harga: int, persen: float | None = None
):
  if harga <= 0:
    await interaction.response.send_message(
        "❌ Harga harus lebih dari 0.", ephemeral=True
    )
    return

  pakai_persen = persen if persen is not None else MM_FEE_PERCENT
  fee, total = hitung_fee(harga, pakai_persen)
  embed = discord.Embed(
      title="🧮 Kalkulator Fee Middleman",
      color=discord.Color.blue(),
  )
  embed.add_field(name="Harga Barang", value=f"Rp {harga:,}", inline=True)
  embed.add_field(name=f"Fee ({pakai_persen:g}%)", value=f"Rp {fee:,}", inline=True)
  embed.add_field(name="Total Transfer", value=f"Rp {total:,}", inline=False)
  embed.set_footer(
      text="Ini cuma kalkulator manual/referensi -- tax beneran di tiap "
      "tiket tetap diisi Middleman lewat tombol 'Set Tax'."
  )

  await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.command()
@commands.has_permissions(administrator=True)
async def setup_ticket(ctx):
  embed = discord.Embed(
      title="🛡️ XypherStore Middleman Service",
      description=(
          "Mau pakai jasa Middleman (MM)?\nKlik tombol di bawah untuk membuat"
          " **Private Channel** transaksi secara otomatis!"
      ),
      color=discord.Color.blue(),
  )
  embed.set_footer(text="XypherStore Automated System")
  await ctx.send(embed=embed, view=TicketButton())
  await ctx.message.delete()


bot.run(TOKEN)